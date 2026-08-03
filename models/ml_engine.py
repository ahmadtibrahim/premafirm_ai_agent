"""
Core ML engine — GPT + knowledge-base RAG.
All generate_* methods follow the same pattern:
  1. Gather context (source record fields + attachment text)
  2. Search knowledge base for similar past examples
  3. Build a few-shot prompt (examples teach the model from past corrections)
  4. Call GPT-5.5 (or configured model)
  5. Create a premafirm.ml.draft record and return it
"""
import base64
import json
import logging
import re

import requests
from odoo import api, models

from odoo.addons.premafirm_ai_engine.services.deepseek_utils import (
    deepseek_chat as _deepseek_chat,
    get_api_key as _get_deepseek_key,
    get_model as _get_deepseek_model,
    today_context_line as _today_context_line,
)
from odoo.addons.premafirm_ai_engine.services.openai_utils import openai_chat as _openai_chat

_logger = logging.getLogger(__name__)


class PremafirmMLEngine(models.AbstractModel):
    _name = 'premafirm.ml.engine'
    _description = 'ML Engine (GPT + RAG)'

    # ── Config helpers ─────────────────────────────────────────────

    def _api_key(self):
        return _get_deepseek_key(self.env)

    def _model(self):
        return _get_deepseek_model(self.env)

    def _vision_api_key(self):
        """Vision extraction needs OpenAI — DeepSeek's chat API rejects image_url content."""
        return (self.env['ir.config_parameter'].sudo().get_param('openai.api_key') or '').strip()

    # ── Claude call ───────────────────────────────────────────────

    def _gpt(self, system_prompt, user_prompt, max_tokens=1024):
        """Calls OpenAI. Name kept as _gpt for backward compatibility."""
        api_key = self._api_key()
        if not api_key:
            return None, '⚠ DeepSeek API key not configured. Set deepseek.api_key in Settings.'
        model = self._model()
        try:
            text = _deepseek_chat(
                messages=[{'role': 'user', 'content': user_prompt}],
                system=system_prompt,
                max_tokens=max_tokens,
                api_key=api_key,
                model=model,
                timeout=90,
            )
            return text, None
        except Exception as e:
            return None, str(e)

    # ── Attachment text extraction ─────────────────────────────────

    def _attachment_texts(self, model_name, record_id):
        """Extract readable text from all attachments on a record."""
        atts = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', model_name),
            ('res_id', '=', record_id),
            ('type', '=', 'binary'),
        ])
        parts = []
        for att in atts:
            if not att.datas:
                continue
            text = self._extract_file(att.datas, att.mimetype or '', att.name or '')
            if text:
                parts.append(f'[Attachment: {att.name}]\n{text[:3000]}')
        return parts

    def _extract_file(self, b64_data, mimetype, filename):
        """
        Extract text from a base64-encoded file using the zero-cost document extractor.
        Supports PDF (text layer + OCR fallback), images (OCR), Excel, CSV, and plain text.
        """
        try:
            from odoo.addons.premafirm_ai_engine.services import document_extractor
            text, _method = document_extractor.extract_from_b64(b64_data, mimetype, filename)
            return text[:5000]
        except Exception as e:
            _logger.debug('_extract_file via document_extractor failed: %s', e)
        return ''

    # ── Few-shot prompt builder ────────────────────────────────────

    def _build_examples(self, knowledge_type, query_text):
        examples = self.env['premafirm.ml.knowledge']._search_similar(
            knowledge_type, query_text, limit=6)
        if not examples:
            return '', 0

        parts = ['--- PAST EXAMPLES (learn from these) ---']
        for i, ex in enumerate(examples, 1):
            parts.append(f'\nExample {i}:')
            parts.append(f'  Situation: {(ex.input_context or "")[:400]}')
            parts.append(f'  Good output: {(ex.good_output or "")[:400]}')
            if ex.correction_note:
                parts.append(f'  Note: {ex.correction_note[:200]}')
        parts.append('--- END EXAMPLES ---')
        return '\n'.join(parts), len(examples)

    def _create_draft(self, draft_type, source_model, source_id,
                      suggestion, reasoning, context_snapshot, examples_used=0):
        return self.env['premafirm.ml.draft'].create({
            'draft_type':       draft_type,
            'source_model':     source_model,
            'source_id':        source_id,
            'ai_suggestion':    suggestion,
            'ai_reasoning':     reasoning,
            'context_snapshot': context_snapshot,
            'examples_used':    examples_used,
        })

    # ====================================================================
    # Feature: Load Tender Extraction (Vision AI)
    # ====================================================================

    @staticmethod
    def _load_tender_stop_rules():
        return (
            "\n- type must be 'pickup' or 'delivery'."
            "\n- Extract ALL stops in sequence order."
            "\n- The first origin/shipper/loading/pickup stop is usually 'pickup'."
            "\n- Consignee/delivery/drop/final/customer receiving stops must be 'delivery'."
            "\n- Do not mark every stop as pickup unless the document explicitly shows multiple pickups and no deliveries."
            "\n- If the attached message text adds a pickup address, delivery address, or time window, merge that with the document."
            "\n- pickup_time = scheduled time or time window of the FIRST pickup stop."
        )

    def extract_load_tender(self, image_b64, mimetype, message_text=''):
        """
        Use GPT-4o vision to extract structured freight data from a load tender image.
        Returns a dict or None on failure.
        """
        api_key = self._api_key()
        if not api_key:
            return None

        vision_model = self.env['ir.config_parameter'].sudo().get_param(
            'prema_ai.vision_model', 'gpt-4o')
        vision_key = self._vision_api_key()

        # Build data URL for the image
        mime = (mimetype or 'image/jpeg').split(';')[0].strip()
        if 'pdf' in mime:
            # PDF: try text extraction instead of vision
            text = self._extract_file(image_b64, mimetype, 'load_tender.pdf')
            if text:
                merged_text = text
                if message_text:
                    merged_text += '\n\n[WhatsApp message context]\n' + message_text[:1500]
                return self._extract_load_tender_from_text(merged_text, api_key, self._model())
            return None

        # ── Try free OCR before paying for vision ─────────────────────
        try:
            from odoo.addons.premafirm_ai_engine.services import document_extractor
            raw = base64.b64decode(image_b64)
            ocr_text, ocr_method = document_extractor.extract_text(raw, mime, 'attachment')
            if ocr_text and len(ocr_text) >= 120:
                _logger.info('extract_load_tender: using %s path (no vision API)', ocr_method)
                merged = ocr_text
                if message_text:
                    merged += '\n\n[WhatsApp message context]\n' + message_text[:1500]
                result = self._extract_load_tender_from_text(merged, api_key, self._model())
                if result and result.get('stops'):
                    return result
                # OCR text was present but GPT couldn't parse it cleanly — fall through to vision
        except Exception as e:
            _logger.debug('OCR pre-check failed, falling back to vision: %s', e)
        # ── Vision API fallback (only when OCR fails or yields no stops) ──

        data_url = f"data:{mime};base64,{image_b64.decode() if isinstance(image_b64, bytes) else image_b64}"

        prompt = (
            f"{_today_context_line()} "
            "Extract all freight load tender / bill of lading data from this image. "
            "Return ONLY valid JSON with this exact structure (no markdown, no extra text):\n"
            '{"equipment_type":"","commodity":"","total_weight_lbs":0,"service_type":"Multi-stop delivery",'
            '"offered_rate":0,"pickup_company":"","fuel_included":true,"liftgate_included":false,'
            '"reefer_or_dry":"dry",'
            '"pickup_time":"'
            '",'
            '"stops":[{"type":"pickup","company_name":"","street":"","city":"","province":"",'
            '"po_number":"","load_tender_ref":"","pallets":0,"weight_lbs":0,'
            '"scheduled_time":"","liftgate":false,"special_instructions":""}]}'
            "\n\nRules:"
            "\n- offered_rate = 0 if not shown."
            "\n- reefer_or_dry: 'reefer' if refrigerated/frozen/chilled load, otherwise 'dry'."
            "\n- total_weight_lbs: sum of all stop weights ONLY if explicitly shown on the document. If weight is not stated, set total_weight_lbs to 0 and all stop weight_lbs to 0. Never estimate weight from pallet or box counts."
            + self._load_tender_stop_rules()
        )
        if message_text:
            prompt += "\n\nWhatsApp message text sent with this tender:\n" + message_text[:1500]

        if not vision_key:
            _logger.warning('extract_load_tender: no openai.api_key configured for vision fallback')
            return None
        try:
            content = _openai_chat(
                messages=[{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': data_url, 'detail': 'high'}},
                    {'type': 'text', 'text': prompt},
                ]}],
                max_tokens=1500,
                api_key=vision_key,
                model=vision_model,
                timeout=60,
            )
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            return json.loads(content)
        except Exception as e:
            _logger.warning('extract_load_tender failed: %s', e)
            return None

    def _extract_load_tender_from_text(self, text, api_key, model):
        """Fallback: extract load tender data from plain text (e.g. PDF)."""
        prompt = (
            f"{_today_context_line()} "
            "Extract all freight load tender data from the text below. "
            "Return ONLY valid JSON with this structure (no markdown):\n"
            '{"equipment_type":"","commodity":"","total_weight_lbs":0,'
            '"service_type":"Multi-stop delivery","offered_rate":0,'
            '"pickup_company":"","fuel_included":true,"liftgate_included":false,'
            '"reefer_or_dry":"dry","pickup_time":"",'
            '"stops":[{"type":"pickup","company_name":"","street":"","city":"",'
            '"province":"","po_number":"","load_tender_ref":"","pallets":0,'
            '"weight_lbs":0,"scheduled_time":"","liftgate":false,'
            '"special_instructions":""}]}'
            "\n\nRules:"
            "\n- reefer_or_dry: 'reefer' if refrigerated/frozen/chilled load, otherwise 'dry'."
            "\n- total_weight_lbs: sum all weights ONLY if explicitly shown. If weight is not stated, set total_weight_lbs to 0 and all stop weight_lbs to 0. Never estimate from pallet or box counts."
            + self._load_tender_stop_rules()
            + "\n\nText:\n" + text[:3000]
        )
        try:
            content = _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=1200,
                api_key=api_key,
                model=model,
                timeout=45,
            )
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            return json.loads(content)
        except Exception:
            return None

    def extract_load_tender_from_chat(self, text):
        """
        Try to extract structured load tender data from a plain-text WA chat message.
        Returns the same dict schema as extract_load_tender, or None if not a load tender.
        If the message is just a rate offer or generic chat, GPT returns {"stops":[]} → None.
        """
        if not text or len(text) < 40:
            return None
        api_key = self._api_key()
        if not api_key:
            return None
        prompt = (
            f"{_today_context_line()} "
            "Read this WhatsApp message from a freight dispatcher. "
            "If it contains actual load tender / freight job details (stops, addresses, pickup/delivery locations), "
            "extract them into JSON. "
            "If it is ONLY a rate offer, short reply, or generic chat with no load details, return {\"stops\":[]}.\n"
            "Return ONLY valid JSON — no markdown, no extra text:\n"
            '{"equipment_type":"","commodity":"","total_weight_lbs":0,'
            '"service_type":"Multi-stop delivery","offered_rate":0,'
            '"pickup_company":"","fuel_included":true,"liftgate_included":false,'
            '"reefer_or_dry":"dry","pickup_time":"",'
            '"stops":[{"type":"pickup","company_name":"","street":"","city":"",'
            '"province":"","po_number":"","load_tender_ref":"","pallets":0,'
            '"weight_lbs":0,"scheduled_time":"","liftgate":false,'
            '"special_instructions":""}]}'
            "\n\nRules: reefer_or_dry = 'reefer' if cold/frozen load."
            + self._load_tender_stop_rules()
            + "\n\nMessage:\n" + text[:2500]
        )
        try:
            content = _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=1200,
                api_key=api_key,
                model=self._model(),
                timeout=45,
            )
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            result = json.loads(content)
            return result if result.get('stops') else None
        except Exception as e:
            _logger.warning('extract_load_tender_from_chat failed: %s', e)
            return None

    @staticmethod
    def detect_negotiation_intent(text):
        """
        Classify a short WhatsApp message as a negotiation signal.
        Returns {'type': 'offer'|'counter'|'agreement'|'decline'|'none', 'amount': float|None}
        No GPT needed — pure regex.
        """
        t = (text or '').strip().lower()
        t_clean = re.sub(r'[^\w\s$.,]', ' ', t)

        # Extract dollar amount if present
        amount_match = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', t_clean)
        amount = float(amount_match.group(1).replace(',', '')) if amount_match else None

        # Agreement signals — including WA button-reply titles
        agreement_patterns = [
            r'^yeah\b', r'^yes\b', r'^yep\b', r'^yup\b', r'^ok\b',
            r'^okay\b', r'^sure\b', r'^deal\b', r'^agreed\b',
            r'^sounds good', r'^perfect\b', r'^works\b', r'^confirmed\b',
            r"^that'?s? (fine|good|great|works)", r'^no problem\b',
            r'^approve\b',      # WA button reply: "Approve ✅"
        ]
        for pat in agreement_patterns:
            if re.search(pat, t_clean):
                return {'type': 'agreement', 'amount': amount}

        # Decline signals — including WA button-reply titles
        decline_patterns = [
            r'\bno\b', r"\bcan'?t\b", r'\bpass\b', r'\bnot available\b',
            r'\btoo low\b', r'\bnot interested\b', r'\bsorry\b.*\bno\b',
            r'\bnot going to work\b',
            r'^cancel\b',       # WA button reply: "Cancel ❌"
        ]
        for pat in decline_patterns:
            if re.search(pat, t_clean):
                return {'type': 'decline', 'amount': None}

        edit_patterns = [
            r'^edit\b',
            r'^change\b',
            r'^revise\b',
            r'^update\b',
            r'^modify\b',
        ]
        for pat in edit_patterns:
            if re.search(pat, t_clean):
                return {'type': 'edit', 'amount': amount}

        # Offer / counter signals — must have a dollar amount
        if amount:
            offer_patterns = [
                r'can you do', r'how about', r'what about', r'do\s+\$',
                r'i can do', r"i'll do", r'will do', r'can do',
                r'i can offer', r'my best', r'all in',
            ]
            for pat in offer_patterns:
                if re.search(pat, t_clean):
                    return {'type': 'offer', 'amount': amount}
            # Bare dollar amount alone (e.g. "600" or "$600") = counter
            if re.match(r'^\$?\s*[\d,]+(?:\.\d{1,2})?\s*(?:all in|cad|usd)?$', t_clean.strip()):
                return {'type': 'counter', 'amount': amount}

        return {'type': 'none', 'amount': None}

    # ====================================================================
    # Feature: Rate Quote
    # ====================================================================

    def _cost_params_text(self):
        """Read the estimator system parameters and return a formatted string for GPT context."""
        p = self.env['ir.config_parameter'].sudo()
        fuel        = float(p.get_param('estimator.fuel_price_per_l',         '1.55'))
        driver      = float(p.get_param('estimator.driver_rate_per_hr',        '28.00'))
        margin      = float(p.get_param('estimator.margin_pct',                '20.0'))
        wt_thresh   = float(p.get_param('estimator.weight_threshold_lbs',      '3000'))
        wt_cwt      = float(p.get_param('estimator.weight_surcharge_per_cwt',  '5.00'))
        return (
            f"PremaFirm current cost parameters:\n"
            f"  • Fuel: ${fuel:.3f}/L\n"
            f"  • Driver: ${driver:.2f}/hr\n"
            f"  • Margin target: {margin:.1f}%\n"
            f"  • Weight surcharge: ${wt_cwt:.2f} per CWT on load over {wt_thresh:,.0f} lbs\n"
            f"Use these as the basis for any cost estimates. "
            f"The suggested rate = total cost × (1 + {margin:.0f}%)."
        )

    def generate_rate_quote(self, message_text, partner=None, source_model=None, source_id=None):
        """
        Generate a freight rate estimate draft from a text request.
        Learns from past approved rate quotes in the knowledge base.
        Uses live cost parameters (fuel, driver rate, margin) from system settings.
        """
        att_texts = []
        if source_model and source_id:
            att_texts = self._attachment_texts(source_model, source_id)

        partner_info = ''
        if partner:
            partner_info = (
                f'Customer: {partner.name}'
                + (f' | Company: {partner.parent_id.name}' if partner.parent_id else '')
                + (f' | Country: {partner.country_id.name}' if partner.country_id else '')
            )

        context = message_text
        if att_texts:
            context += '\n\n' + '\n\n'.join(att_texts)

        examples_text, n_examples = self._build_examples('rate_quote', context)
        cost_params = self._cost_params_text()

        system = (
            'You are a freight pricing specialist for PremaFirm Logistics. '
            'Generate a professional rate quote draft based on the request and any attachments.\n\n'
            + cost_params + '\n\n'
            'Include: estimated rate (CAD), transit time, service type (LTL/FTL), '
            'any assumptions made, and a polite note that the final rate is subject to confirmation. '
            'Be concise and professional. Base your rate on the cost parameters above plus '
            'the past examples provided. Do not invent rates with no basis.'
        )
        user = f'{examples_text}\n\n{partner_info}\n\nRate Request:\n{context}'

        text, err = self._gpt(system, user, max_tokens=600)
        if err:
            return None, err

        draft = self._create_draft(
            draft_type='rate_quote',
            source_model=source_model or '',
            source_id=source_id or 0,
            suggestion=text,
            reasoning=f'Generated from {n_examples} similar past quotes.',
            context_snapshot=json.dumps({'message': message_text[:500],
                                         'partner': partner_info,
                                         'attachments': len(att_texts)}),
            examples_used=n_examples,
        )
        return draft, None

    # ====================================================================
    # Feature: WhatsApp Reply
    # ====================================================================

    def generate_negotiation_reply(self, neg):
        """
        Generate a short professional reply to a dispatcher for a WA negotiation.
        Called only when staff explicitly clicks 'AI Rewrite Reply' — not automatic.
        """
        api_key = self._api_key()
        if not api_key:
            return None

        pickup = neg.stop_ids.filtered(lambda s: s.stop_type == 'pickup')[:1]
        deliveries = neg.stop_ids.filtered(lambda s: s.stop_type == 'delivery')
        route = ''
        if pickup:
            route = pickup.city or pickup.company_name or ''
        if deliveries:
            last_city = deliveries[-1:].city or deliveries[-1:].company_name or ''
            route = f"{route} → {last_city}" if route else last_city

        ctx_lines = [
            f"Route: {route or 'TBD'}",
            f"Stops: {neg.stops_count}",
            f"Equipment: {neg.equipment_type or 'TBD'}",
            f"Commodity: {neg.commodity or 'General freight'}",
            f"Weight: {neg.total_weight_lbs:.0f} lbs" if neg.total_weight_lbs else '',
            f"Their offer: ${neg.their_offer:,.0f}" if neg.their_offer else '',
            f"Our rate: ${neg.our_counter:,.0f}" if neg.our_counter else
            (f"Estimated rate: ${neg.suggested_rate:,.0f}" if neg.suggested_rate else ''),
            f"Reefer temp: {neg.reefer_temp}" if neg.reefer_temp else '',
            f"First pickup window: {neg.pickup_time}" if neg.pickup_time else '',
        ]
        context = '\n'.join(l for l in ctx_lines if l)

        prompt = (
            "You are a freight dispatcher at PremaFirm Logistics replying via WhatsApp. "
            "This is a VERBAL RATE OFFER during negotiation — NOT a formal quotation confirmation. "
            "Write a SHORT, direct rate offer message (2-4 lines max). "
            "State the rate we can do this at. Keep it conversational and professional. "
            "Do NOT say 'quotation is ready', do NOT mention PDF, do NOT mention buttons. "
            "Do NOT mention fuel, liftgate, detention unless the customer specifically asked. "
            "End with a simple call to action like 'Let me know if this works for you.' or 'Does this rate work?' "
            "Freight industry tone. No emojis. No greeting headers.\n\n"
            f"Load details:\n{context}\n\nWrite the verbal rate offer reply:"
        )
        try:
            return _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=200,
                api_key=api_key,
                model=self._model(),
                timeout=30,
            )
        except Exception as e:
            _logger.warning('generate_negotiation_reply failed: %s', e)
            return None

    def rewrite_negotiation_reply(self, neg, current_text):
        """Rewrite the existing draft reply while keeping the current quotation context."""
        api_key = self._api_key()
        if not api_key or not current_text:
            return None
        context = neg._ml_context()
        prompt = (
            "Rewrite this WhatsApp freight reply so it is cleaner and more professional. "
            "Keep it short, direct, and customer-facing. "
            "Do not mention fuel included, liftgate, detention, or back-office details unless essential. "
            "Keep the meaning, but improve clarity and tone.\n\n"
            f"Negotiation context:\n{context[:1800]}\n\n"
            f"Current draft:\n{current_text[:1800]}\n\n"
            "Return only the rewritten reply."
        )
        try:
            return _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=250,
                api_key=api_key,
                model=self._model(),
                timeout=30,
            )
        except Exception:
            return None

    def rewrite_modification_notes(self, neg, current_text):
        """Rewrite internal modification notes into cleaner, reusable staff notes."""
        api_key = self._api_key()
        if not api_key or not current_text:
            return None
        prompt = (
            "Rewrite these internal freight quotation modification notes for staff use. "
            "Keep all operational meaning, but organize the notes clearly for future ML learning. "
            "Prioritize customer-specific preferences, stop corrections, pickup time corrections, "
            "and any repeating mistakes to avoid. Use short plain lines, not long paragraphs.\n\n"
            f"Negotiation context:\n{neg._ml_context()[:1800]}\n\n"
            f"Current notes:\n{current_text[:2500]}\n\n"
            "Return only the rewritten notes."
        )
        try:
            return _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=500,
                api_key=api_key,
                model=self._model(),
                timeout=30,
            )
        except Exception:
            return None

    def generate_budget_negotiation_reply(self, neg, customer_budget):
        """
        Generate a professional WA reply when the dispatcher proposes a budget.
        If the budget is close enough we accept/bridge; if too low we hold the line
        with a polite but firm counter.
        """
        api_key = self._api_key()
        if not api_key:
            return None

        our_rate = neg.our_counter or neg.suggested_rate or neg.estimated_cost or 0
        route = ' → '.join(
            s.city or s.company_name
            for s in neg.stop_ids if s.city or s.company_name
        ) or 'TBD'
        is_feasible = (customer_budget >= our_rate * 0.90) if our_rate else True

        if is_feasible:
            stance = (
                f"The customer's budget (${customer_budget:,.0f}) is close to or meets our rate "
                f"(${our_rate:,.0f}). Write a short, friendly WA reply confirming we can work "
                f"with that number or meeting them at exactly our rate of ${our_rate:,.0f}. "
                f"Keep it 2-3 lines. End by asking them to confirm so we can proceed."
            )
        else:
            stance = (
                f"The customer's budget (${customer_budget:,.0f}) is below our minimum "
                f"(${our_rate:,.0f}). Write a short, firm but respectful WA reply that:\n"
                f"1. Acknowledges their budget\n"
                f"2. Explains briefly why that price isn't feasible for this load "
                f"(distance, multi-stop, fuel, etc.) — don't be apologetic\n"
                f"3. Holds the line at ${our_rate:,.0f} as the best we can do\n"
                f"4. Keeps the door open for future loads\n"
                f"3-4 lines max. Freight industry tone. No hollow filler phrases."
            )

        prompt = (
            f"You are a freight dispatcher at PremaFirm Logistics negotiating via WhatsApp.\n"
            f"Route: {route} ({neg.stops_count} stop{'s' if neg.stops_count != 1 else ''})\n"
            f"Commodity: {neg.commodity or 'general freight'} | "
            f"Equipment: {neg.equipment_type or 'dry van'}\n"
            f"Customer budget: ${customer_budget:,.0f} CAD | Our rate: ${our_rate:,.0f} CAD\n\n"
            + stance
        )
        try:
            return _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=220,
                api_key=api_key,
                model=self._model(),
                timeout=30,
            )
        except Exception as e:
            _logger.warning('generate_budget_negotiation_reply failed: %s', e)
            return None

    def suggest_quote_update_notes(self, neg, customer_message):
        """Convert a customer's edit request into concise staff update notes."""
        api_key = self._api_key()
        if not api_key or not customer_message:
            return None
        prompt = (
            "A customer requested edits to a freight quotation. "
            "Summarize the requested changes into short internal staff notes. "
            "Call out any stop type correction, pickup location correction, pickup time window, reefer/dry change, "
            "rate change, or other operational correction. If the customer text sounds like a completely new load request, "
            "say so explicitly on the first line.\n\n"
            f"Negotiation context:\n{neg._ml_context()[:1800]}\n\n"
            f"Customer message:\n{customer_message[:1800]}\n\n"
            "Return only the internal note text."
        )
        try:
            return _deepseek_chat(
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=220,
                api_key=api_key,
                model=self._model(),
                timeout=30,
            )
        except Exception:
            return None

    def generate_wa_reply(self, channel, incoming_message, attachment_texts=None):
        """Draft a reply to an incoming WhatsApp message."""
        att_texts = attachment_texts or []
        if channel.source_model if hasattr(channel, 'source_model') else False:
            att_texts += self._attachment_texts(channel.source_model, channel.source_id)

        partner = channel.whatsapp_partner_id
        partner_info = f'Contact: {partner.name}' if partner else ''
        if partner and partner.parent_id:
            partner_info += f' | Company: {partner.parent_id.name}'

        # Last 6 messages for context
        recent = self.env['mail.message'].search([
            ('res_id', '=', channel.id),
            ('model', '=', 'discuss.channel'),
            ('message_type', 'in', ['comment', 'whatsapp_message']),
        ], order='date desc', limit=6)
        history = '\n'.join(
            f"  [{m.author_id.name or 'Unknown'}]: {(m.body or '').replace('<p>', '').replace('</p>', '')[:200]}"
            for m in reversed(recent)
        )

        context = f'{partner_info}\n\nConversation history:\n{history}\n\nLatest message:\n{incoming_message}'
        if att_texts:
            context += '\n\nAttachments:\n' + '\n\n'.join(att_texts)

        examples_text, n_examples = self._build_examples('wa_reply', incoming_message)

        is_rate_request = any(kw in incoming_message.lower() for kw in [
            'rate', 'quote', 'price', 'cost', 'how much', 'shipping',
            'freight', 'truck', 'ltl', 'ftl', 'delivery', 'pickup',
        ])

        if is_rate_request:
            rate_draft, err = self.generate_rate_quote(
                incoming_message, partner=partner,
                source_model='discuss.channel', source_id=channel.id)
            system = (
                'You are a logistics coordinator at PremaFirm. '
                'Draft a professional WhatsApp reply to a rate inquiry. '
                'Keep it concise and mobile-friendly (no long paragraphs). '
                'Include the rate estimate from context. End with an offer to confirm details.'
            )
            rate_text = rate_draft.ai_suggestion if rate_draft else 'Rate to be confirmed.'
            user = f'{examples_text}\n\n{context}\n\nRate estimate to include:\n{rate_text}'
        else:
            system = (
                'You are a logistics coordinator at PremaFirm. '
                'Draft a helpful, professional WhatsApp reply. '
                'Keep it concise and mobile-friendly. Match the tone of the conversation.'
            )
            user = f'{examples_text}\n\n{context}'

        text, err = self._gpt(system, user, max_tokens=400)
        if err:
            return None, err

        draft = self._create_draft(
            draft_type='wa_reply',
            source_model='discuss.channel',
            source_id=channel.id,
            suggestion=text,
            reasoning=f'{"Rate request detected. " if is_rate_request else ""}Used {n_examples} similar WA examples.',
            context_snapshot=json.dumps({'message': incoming_message[:500],
                                         'partner': partner_info,
                                         'is_rate_request': is_rate_request}),
            examples_used=n_examples,
        )
        return draft, None

    # ====================================================================
    # Feature: CRM Reply
    # ====================================================================

    def generate_crm_reply(self, lead):
        """Draft a reply to the latest message on a CRM lead."""
        # Get last incoming message
        last_msg = self.env['mail.message'].search([
            ('res_id', '=', lead.id),
            ('model', '=', 'crm.lead'),
            ('message_type', 'in', ['email', 'comment']),
            ('author_id', '!=', self.env.ref('base.partner_root').id),
        ], order='date desc', limit=1)

        msg_text = ''
        if last_msg:
            msg_text = re.sub(r'<[^>]+>', ' ', last_msg.body or '').strip()

        att_texts = self._attachment_texts('crm.lead', lead.id)

        context = (
            f'Lead: {lead.name}\n'
            f'Company: {lead.partner_name or lead.partner_id.name or ""}\n'
            f'Stage: {lead.stage_id.name if lead.stage_id else ""}\n'
            f'Outreach stage: {lead.outreach_stage or ""}\n'
            f'Latest message:\n{msg_text or "(no message found)"}'
        )
        if att_texts:
            context += '\n\nAttachments:\n' + '\n\n'.join(att_texts)

        examples_text, n_examples = self._build_examples('crm_reply', msg_text or lead.name)

        system = (
            'You are a business development representative at PremaFirm Logistics. '
            'Draft a professional, personalized reply to the lead\'s latest message. '
            'Be helpful, concise, and move the conversation forward. '
            'Do not use generic filler phrases.'
        )
        user = f'{examples_text}\n\n{context}'

        text, err = self._gpt(system, user, max_tokens=500)
        if err:
            return None, err

        draft = self._create_draft(
            draft_type='crm_reply',
            source_model='crm.lead',
            source_id=lead.id,
            suggestion=text,
            reasoning=f'Based on latest message from {lead.partner_name or "contact"}. Used {n_examples} examples.',
            context_snapshot=json.dumps({'lead': lead.name,
                                         'stage': lead.stage_id.name if lead.stage_id else '',
                                         'message': msg_text[:500]}),
            examples_used=n_examples,
        )
        return draft, None

    # ====================================================================
    # Feature: Invoice Flagging
    # ====================================================================

    def flag_invoice(self, invoice):
        """Analyse an invoice for anomalies. Returns a draft only if something looks off."""
        lines_summary = '\n'.join(
            f'  - {l.name or l.product_id.name or "Line"}: {l.quantity} × {l.price_unit} {invoice.currency_id.name}'
            for l in invoice.invoice_line_ids[:15]
        )
        att_texts = self._attachment_texts('account.move', invoice.id)

        context = (
            f'Invoice: {invoice.name}\n'
            f'Vendor/Customer: {invoice.partner_id.name if invoice.partner_id else "Unknown"}\n'
            f'Type: {invoice.move_type}\n'
            f'Amount: {invoice.amount_total} {invoice.currency_id.name}\n'
            f'Date: {invoice.invoice_date}\n'
            f'Journal: {invoice.journal_id.name if invoice.journal_id else ""}\n'
            f'Lines:\n{lines_summary}'
        )
        if att_texts:
            context += '\n\nAttachment content:\n' + '\n\n'.join(att_texts)

        examples_text, n_examples = self._build_examples('invoice_flag', context)

        system = (
            'You are a financial auditor reviewing invoices for anomalies. '
            'Analyse the invoice and decide if it needs flagging. '
            'Only flag if there is a genuine concern: unusual amount, duplicate risk, '
            'missing info, suspicious line items, or mismatch with attachments. '
            'If everything looks normal, reply with exactly: NO_FLAG\n'
            'If flagging, reply with a short 2-3 sentence explanation of the concern. '
            'Be specific — mention amounts, vendor names, or line items.'
        )
        user = f'{examples_text}\n\n{context}'

        text, err = self._gpt(system, user, max_tokens=300)
        if err or not text or text.strip().upper() == 'NO_FLAG':
            return None, err

        draft = self._create_draft(
            draft_type='invoice_flag',
            source_model='account.move',
            source_id=invoice.id,
            suggestion=text,
            reasoning=f'Auto-flagged during posting. Used {n_examples} past examples.',
            context_snapshot=json.dumps({'invoice': invoice.name,
                                         'amount': float(invoice.amount_total),
                                         'partner': invoice.partner_id.name if invoice.partner_id else ''}),
            examples_used=n_examples,
        )
        return draft, None

    # ====================================================================
    # Feature: Bill Auto-Fill from Attachment (zero-cost extraction)
    # ====================================================================

    def generate_bill_autofill(self, b64_data, mimetype, filename, invoice_record=None):
        """
        Extract vendor bill fields from an attachment using free OCR/PDF parsing,
        then call the text model (not vision) to structure the result as JSON.
        Returns (draft, error_message).
        """
        from odoo.addons.premafirm_ai_engine.services import document_extractor

        text, method = document_extractor.extract_from_b64(b64_data, mimetype, filename)
        if not text or len(text) < 50:
            return None, (
                f'Could not extract readable text from the attachment '
                f'(method tried: {method}). '
                f'Try a cleaner scan or a text-based PDF.'
            )

        vendor_hint = self._guess_vendor_from_text(text[:600])
        query = f"{vendor_hint}\n{text[:400]}"
        examples_text, n_examples = self._build_examples('bill_import', query)

        # Explicit vendor profile lookup — highest-priority guide for known vendors
        vendor_profile_text = ''
        if vendor_hint:
            profile_entry = self.env['premafirm.ml.knowledge'].search([
                ('knowledge_type', '=', 'bill_import'),
                ('input_context', '=', f'VENDOR_PROFILE: {vendor_hint}'),
            ], limit=1)
            if not profile_entry:
                # Fuzzy: try first 20 chars of vendor name
                profile_entry = self.env['premafirm.ml.knowledge'].search([
                    ('knowledge_type', '=', 'bill_import'),
                    ('input_context', 'ilike', f'VENDOR_PROFILE: {vendor_hint[:20]}'),
                ], limit=1)
            if profile_entry:
                try:
                    vp = json.loads(profile_entry.good_output)
                    top_prods = ', '.join(
                        f"{p['name']} (×{p['count']})"
                        for p in vp.get('top_products', [])
                    )
                    vendor_profile_text = (
                        f'\n\nVENDOR PROFILE — {vp.get("vendor_name", vendor_hint)}:\n'
                        f'  Default product : {vp.get("default_product") or "none"}\n'
                        f'  Default account : {vp.get("default_account") or "none"}\n'
                        f'  Default tax     : {vp.get("default_tax") or "none"}\n'
                        f'  All products seen: {top_prods or "none"}\n'
                        f'Use these as the PRIMARY defaults for every line on this bill '
                        f'unless the document clearly indicates a different product/account.'
                    )
                except Exception:
                    pass

        system = (
            'You are an invoice processing assistant at PremaFirm Logistics (Canadian trucking). '
            'Extract all vendor bill data from the document text below. '
            'Return ONLY valid JSON — no markdown, no extra text:\n'
            '{"vendor_name":"","invoice_number":"","invoice_date":"YYYY-MM-DD",'
            '"due_date":"YYYY-MM-DD","currency":"CAD","subtotal":0,"tax_amount":0,'
            '"total_amount":0,"payment_terms":"","reference":"","delivery_number":"",'
            '"fuel_type":"","station_name":"","station_address":"","station_city":"",'
            '"station_province":"","station_postal_code":"",'
            '"line_items":[{"description":"","quantity":1,"unit_price":0,"amount":0,'
            '"suggested_account_code":"","tax_code":""}]}\n\n'
            'Rules:\n'
            '- Dates: YYYY-MM-DD format or empty string if not found.\n'
            '- FUEL RECEIPTS: Use the line label exactly as shown (e.g. "Fuel sales", "DIESEL"). '
            'Set quantity = litres pumped, unit_price = price-per-litre (pre-tax). '
            'If the receipt says "HST INCLUDED" the subtotal is total / 1.13; '
            'if it says "GST INCLUDED" the subtotal is total / 1.05. '
            'Set fuel_type to "DIESEL", "GASOLINE", or "DEF" as shown.\n'
            '- STATION ADDRESS: Extract the physical address of the station/vendor from '
            'the receipt header (street, city, province/state, postal code). '
            'This is used for IFTA fuel tax reporting so accuracy matters.\n'
            '- Account codes for PremaFirm chart of accounts:\n'
            '  fuel/diesel (Canada) = 610200\n'
            '  fuel/diesel (US) = 610201\n'
            '  repairs & maintenance (Canada) = 610400\n'
            '  repairs & maintenance (US) = 610401\n'
            '  tolls (Canada) = 610300\n'
            '  tolls (US) = 610301\n'
            '  driver expenses/PPE = 610310\n'
            '  insurance & regulatory = 626000\n'
            '  subcontractor/brokerage/load board = 512204\n'
            '  salaries/driver pay = 512110\n'
            '  energy/utilities = 512202\n'
            '  general purchases = 511210\n'
            '  other operating = 512210\n'
            '- Use PAST EXAMPLES first — if a vendor matches an example, copy its account codes exactly.\n'
            '- Tax codes (use exact Odoo tax name):\n'
            '  "13% HST Included" — Ontario/Atlantic receipt where tax IS IN the price shown\n'
            '  "13% HST" — Ontario/Atlantic where tax is ADDED on top\n'
            '  "5% GST Included" — receipt where GST is already in the price shown\n'
            '  "5% GST" — GST added on top\n'
            '  "12% GST+PST BC" — British Columbia\n'
            '  "14% HST" — PEI\n'
            '  "15% HST" — NS/NB/NL\n'
            '  "" (empty) — if no tax shown\n'
            '- If a field cannot be found, use empty string or 0.\n'
            '- Do not invent data — only extract what is in the text.'
        )
        user = f'{vendor_profile_text}{examples_text}\n\nDocument text:\n{text[:4000]}'

        raw_result, err = self._gpt(system, user, max_tokens=1000)
        if err or not raw_result:
            return None, err or 'AI returned no result.'

        try:
            import re as _re
            clean = _re.sub(r'^```(?:json)?\s*', '', raw_result.strip())
            clean = _re.sub(r'\s*```$', '', clean)
            data = json.loads(clean)
        except Exception as e:
            return None, f'Could not parse AI response as JSON: {e}'

        context_snapshot = json.dumps({
            'filename': filename,
            'extraction_method': method,
            'vendor_hint': vendor_hint,
            'text_preview': text[:400],
            'invoice_id': invoice_record.id if invoice_record else 0,
            'extracted': data,
        }, ensure_ascii=False)

        draft = self._create_draft(
            draft_type='bill_autofill',
            source_model='account.move',
            source_id=invoice_record.id if invoice_record else 0,
            suggestion=json.dumps(data, indent=2, ensure_ascii=False),
            reasoning=(
                f'Extracted via {method} — no vision API used. '
                f'Used {n_examples} past bill examples.'
            ),
            context_snapshot=context_snapshot,
            examples_used=n_examples,
        )
        return draft, None

    def generate_rate_conf_autofill(self, b64_data, mimetype, filename, order_record=None):
        """
        Extract customer rate confirmation fields from an attachment using free OCR/PDF parsing,
        then call the text model to structure the result as JSON.
        Returns (draft, error_message).
        """
        from odoo.addons.premafirm_ai_engine.services import document_extractor

        text, method = document_extractor.extract_from_b64(b64_data, mimetype, filename)
        if not text or len(text) < 30:
            return None, (
                f'Could not extract readable text from the attachment '
                f'(method tried: {method}). '
                f'Try a cleaner scan or a text-based PDF.'
            )

        system = (
            'You are a freight rate confirmation parser at PremaFirm Logistics (Canadian trucking). '
            'Extract all relevant data from the rate confirmation document text below. '
            'Return ONLY valid JSON — no markdown, no extra text:\n'
            '{"rate_conf_number":"","customer_name":"","total_rate":0,"currency":"CAD",'
            '"service_type":"","pickup_date":"","pickup_location":"","pickup_company":"",'
            '"delivery_locations":[],"commodity":"","equipment_type":"",'
            '"special_instructions":"","payment_terms":""}\n\n'
            'Rules:\n'
            '- rate_conf_number: the confirmation/reference/PO number shown at the top.\n'
            '- total_rate: the agreed freight charge in dollars (numbers only, no $).\n'
            '- pickup_date: date as YYYY-MM-DD or empty if not found.\n'
            '- delivery_locations: list of city+province strings for each drop stop.\n'
            '- If a field is not found, use empty string or 0.\n'
            '- Do not invent data — only extract what is explicitly in the text.'
        )
        user = f'Document text:\n{text[:4000]}'

        raw_result, err = self._gpt(system, user, max_tokens=600)
        if err or not raw_result:
            return None, err or 'AI returned no result.'

        try:
            import re as _re
            clean = _re.sub(r'^```(?:json)?\s*', '', raw_result.strip())
            clean = _re.sub(r'\s*```$', '', clean)
            data = json.loads(clean)
        except Exception as e:
            return None, f'Could not parse AI response as JSON: {e}'

        context_snapshot = json.dumps({
            'filename': filename,
            'extraction_method': method,
            'text_preview': text[:400],
            'order_id': order_record.id if order_record else 0,
            'extracted': data,
        }, ensure_ascii=False)

        draft = self._create_draft(
            draft_type='rate_conf_autofill',
            source_model='sale.order',
            source_id=order_record.id if order_record else 0,
            suggestion=json.dumps(data, indent=2, ensure_ascii=False),
            reasoning=f'Extracted via {method} — no vision API used.',
            context_snapshot=context_snapshot,
            examples_used=0,
        )
        return draft, None

    @staticmethod
    def _guess_vendor_from_text(text):
        """Heuristic: first substantial non-numeric line is usually the company name."""
        for line in text.split('\n'):
            line = line.strip()
            if len(line) > 5 and not line[0].isdigit() and not line.startswith(('http', 'www')):
                return line
        return ''

    # ====================================================================
    # Feature: Customer Auto-Tagging
    # ====================================================================

    def suggest_customer_tags(self, partner):
        """Suggest contact tags for a partner based on their profile and history."""
        existing_tags = ', '.join(partner.category_id.mapped('name')) or 'none'

        # Gather some interaction history
        messages = self.env['mail.message'].search([
            ('partner_ids', 'in', partner.id),
            ('message_type', 'in', ['email', 'comment']),
        ], order='date desc', limit=5)
        history = '; '.join(
            re.sub(r'<[^>]+>', ' ', m.body or '')[:100] for m in messages) or 'no history'

        context = (
            f'Contact: {partner.name}\n'
            f'Company: {partner.parent_id.name if partner.parent_id else "Individual"}\n'
            f'Industry: {partner.industry_id.name if hasattr(partner, "industry_id") and partner.industry_id else "Unknown"}\n'
            f'Country: {partner.country_id.name if partner.country_id else ""}\n'
            f'Current tags: {existing_tags}\n'
            f'Recent interactions: {history[:400]}'
        )

        # Available tags in the system
        all_tags = self.env['res.partner.category'].search([], limit=50)
        tag_list = ', '.join(all_tags.mapped('name'))

        examples_text, n_examples = self._build_examples('customer_tag', context)

        system = (
            'You are a CRM analyst at PremaFirm Logistics. '
            'Suggest relevant contact tags for this customer from the available tags list. '
            'Only suggest tags that genuinely fit. Return ONLY a comma-separated list of tag names. '
            f'Available tags: {tag_list or "b2b, retail, wholesale, logistics, carrier, broker, 3pl, freight forwarder"}'
        )
        user = f'{examples_text}\n\n{context}'

        text, err = self._gpt(system, user, max_tokens=100)
        if err or not text:
            return None, err

        draft = self._create_draft(
            draft_type='customer_tag',
            source_model='res.partner',
            source_id=partner.id,
            suggestion=text,
            reasoning=f'Auto-tagged based on profile and {len(messages)} recent interactions.',
            context_snapshot=json.dumps({'partner': partner.name,
                                         'existing_tags': existing_tags}),
            examples_used=n_examples,
        )
        return draft, None
