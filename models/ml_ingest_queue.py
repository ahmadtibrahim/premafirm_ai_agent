"""
Phase 2 — ML Ingest Queue
Collects ORM events and asynchronously ingests them into the ML knowledge base.
No GPT calls happen here — data is stored as structured text directly.
"""
import json
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

KC_FOLDER_ID = 189  # Knowledge Center folder


class PremafirmMLIngestQueue(models.Model):
    _name = 'premafirm.ml.ingest.queue'
    _description = 'ML Ingest Queue'
    _order = 'create_date asc'

    model_name = fields.Char(required=True)
    record_id = fields.Integer(required=True)
    operation = fields.Selection(
        [
            ('create', 'Create'),
            ('write', 'Write'),
            ('post', 'Post'),
            ('stage_change', 'Stage Change'),
        ],
        default='write',
    )
    payload = fields.Text(string='JSON Payload')
    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        default='pending',
    )
    error_msg = fields.Char(string='Error')

    # ── Cron: process queue ───────────────────────────────────────
    @api.model
    def action_process_queue(self):
        """Cron entry — process up to 100 pending items oldest first."""
        pending = self.search([('state', '=', 'pending')], limit=100)
        done_count = 0
        error_count = 0

        for item in pending:
            try:
                item._process_item()
                item.state = 'done'
                done_count += 1
            except Exception as exc:
                item.state = 'error'
                item.error_msg = str(exc)[:250]
                error_count += 1
                _logger.exception('ML ingest queue error on item %s: %s', item.id, exc)

        # Purge done items older than 7 days
        cutoff = fields.Datetime.now() - timedelta(days=7)
        old_done = self.search([('state', '=', 'done'), ('create_date', '<', cutoff)])
        purged = len(old_done)
        old_done.unlink()

        _logger.info(
            'ML Ingest Queue: processed %d, errors %d, purged %d old done items.',
            done_count, error_count, purged,
        )

    # ── Item processor ───────────────────────────────────────────
    def _process_item(self):
        self.ensure_one()
        try:
            payload = json.loads(self.payload or '{}')
        except (json.JSONDecodeError, TypeError):
            payload = {}

        knowledge_type = payload.get('knowledge_type', 'general')
        input_context = payload.get('input_context', '')
        good_output = payload.get('good_output', '')
        source_ref = payload.get('source_ref', f'{self.model_name}:{self.record_id}')
        weight = float(payload.get('weight', 1.0))

        # Only store if there is enough text
        combined_text = (input_context or '') + (good_output or '')
        if len(combined_text.strip()) <= 50:
            return  # not enough content — skip silently

        MLKnowledge = self.env['premafirm.ml.knowledge'].sudo()

        # Check for existing entry by source_ref (stored in input_context as prefix)
        existing = MLKnowledge.search(
            [('input_context', 'like', f'[source:{source_ref}]')],
            limit=1,
        )

        vals = {
            'knowledge_type': knowledge_type,
            'input_context': f'[source:{source_ref}]\n{input_context}',
            'good_output': good_output or input_context,
            'weight': weight,
            'origin': 'ingested',
        }

        if existing:
            existing.write(vals)
        else:
            MLKnowledge.create(vals)

        # For company_document type: attempt OCR if attachment_id provided
        if knowledge_type == 'company_document':
            attachment_id = payload.get('attachment_id')
            if attachment_id:
                self._try_ocr_attachment(attachment_id, source_ref)

    def _try_ocr_attachment(self, attachment_id, source_ref):
        """Attempt free OCR via document_extractor service if available."""
        try:
            attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id))
            if not attachment.exists():
                return
            # Try the document extractor if present
            extractor = self.env.get('premafirm.document.extractor')
            if extractor and hasattr(extractor, 'extract_text'):
                text = extractor.extract_text(attachment)
                if text and len(text.strip()) > 100:
                    MLKnowledge = self.env['premafirm.ml.knowledge'].sudo()
                    existing = MLKnowledge.search(
                        [('input_context', 'like', f'[source:{source_ref}]')],
                        limit=1,
                    )
                    ocr_vals = {
                        'knowledge_type': 'company_document',
                        'input_context': f'[source:{source_ref}]\n{text[:1000]}',
                        'good_output': text[:2000],
                        'weight': 1.0,
                        'origin': 'ingested',
                    }
                    if existing:
                        existing.write(ocr_vals)
                    else:
                        MLKnowledge.create(ocr_vals)
        except Exception as exc:
            _logger.warning('OCR attempt failed for attachment %s: %s', attachment_id, exc)

    # ── Knowledge Center document indexer ───────────────────────
    @api.model
    def action_index_kc_documents(self):
        """
        Cron entry — index new documents from the Knowledge Center folder (id=189).
        Uses free text extraction first; GPT (gpt-4.1-nano) only for summarization
        when extracted text > 500 chars.
        """
        try:
            Document = self.env['documents.document'].sudo()
            docs = Document.search([
                ('folder_id', '=', KC_FOLDER_ID),
                ('shortcut_document_id', '=', False),
                ('type', '=', 'binary'),
            ])
        except Exception as exc:
            _logger.warning('KC indexer: cannot query documents: %s', exc)
            return

        MLKnowledge = self.env['premafirm.ml.knowledge'].sudo()
        indexed = 0
        skipped = 0

        for doc in docs:
            source_ref = f'documents.document:{doc.id}'
            already = MLKnowledge.search(
                [
                    ('knowledge_type', '=', 'company_document'),
                    ('input_context', 'like', f'[source:{source_ref}]'),
                ],
                limit=1,
            )
            if already:
                skipped += 1
                continue

            # Extract text
            extracted_text = self._extract_document_text(doc)
            if not extracted_text or len(extracted_text.strip()) < 50:
                skipped += 1
                continue

            good_output = extracted_text

            # Summarize via GPT only when text is substantial
            if len(extracted_text.strip()) > 500:
                summary = self._summarize_with_gpt(extracted_text, doc.name or 'document')
                if summary:
                    good_output = summary

            MLKnowledge.create({
                'knowledge_type': 'company_document',
                'input_context': f'[source:{source_ref}]\nDocument: {doc.name or "Untitled"}',
                'good_output': good_output[:3000],
                'weight': 1.0,
                'origin': 'ingested',
            })
            indexed += 1

        _logger.info('KC Indexer: indexed %d new docs, skipped %d.', indexed, skipped)

    def _extract_document_text(self, doc):
        """Extract plain text from a document attachment using available tools."""
        try:
            if not doc.attachment_id:
                return ''
            attachment = doc.attachment_id
            # Try PDF / text extraction
            if attachment.mimetype and 'text' in attachment.mimetype:
                import base64
                raw = base64.b64decode(attachment.datas or b'')
                return raw.decode('utf-8', errors='replace')[:3000]

            # Try pdfminer / pdfplumber if available
            if attachment.mimetype == 'application/pdf':
                return self._extract_pdf_text(attachment)

            return ''
        except Exception as exc:
            _logger.debug('Text extraction failed for doc %s: %s', doc.id, exc)
            return ''

    def _extract_pdf_text(self, attachment):
        """Attempt PDF text extraction using pdfminer or pdfplumber."""
        try:
            import base64
            raw = base64.b64decode(attachment.datas or b'')
            try:
                import io
                from pdfminer.high_level import extract_text as pdf_extract
                text = pdf_extract(io.BytesIO(raw))
                return (text or '').strip()[:3000]
            except ImportError:
                pass
            try:
                import io
                import pdfplumber
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    pages_text = [p.extract_text() or '' for p in pdf.pages[:10]]
                return '\n'.join(pages_text)[:3000]
            except ImportError:
                pass
            return ''
        except Exception as exc:
            _logger.debug('PDF extraction error: %s', exc)
            return ''

    def _summarize_with_gpt(self, text, doc_name):
        """Use GPT nano to summarize a long document into key facts."""
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            api_key = ICP.get_param('openai.api_key')
            model = ICP.get_param('prema_ai.primary_model', 'gpt-4.1-nano')
            if not api_key:
                return ''

            import sys
            import os
            services_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), 'services'
            )
            if services_path not in sys.path:
                sys.path.insert(0, services_path)
            from openai_utils import openai_chat

            system = (
                'You are a document analyst. Extract the key facts, figures, services, '
                'and policies from this business document in a concise format.'
            )
            user_msg = f'Document: {doc_name}\n\n{text[:2000]}'
            result = openai_chat(
                messages=[{'role': 'user', 'content': user_msg}],
                system=system,
                max_tokens=512,
                api_key=api_key,
                model=model,
            )
            return result
        except Exception as exc:
            _logger.debug('GPT summarization failed: %s', exc)
            return ''
