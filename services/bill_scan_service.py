import json
import logging
import re
import time
from collections import Counter

import requests

from .deepseek_utils import deepseek_chat, get_api_key, get_model

_logger = logging.getLogger(__name__)
BILLS_ENTRY_FOLDER = "bills-entry"
INTER_DOC_DELAY  = 2
RETRY_DELAYS     = [8, 20, 45]


class BillScanService:
    """
    Reads scanned bill images from Odoo Documents 'bills-entry' folder,
    sends each to Claude Haiku Vision, creates a draft vendor bill with
    ML-informed account codes, and saves approved patterns to the knowledge base.
    """

    def __init__(self, env):
        self.env = env

    # ── DeepSeek API helpers ────────────────────────────────────────────────

    def _get_api_key(self):
        key = get_api_key(self.env)
        if not key:
            raise ValueError(
                "DeepSeek API key not configured. Go to Settings → Technical → "
                "System Parameters and add 'deepseek.api_key' or 'deepseek'."
            )
        return key

    def _get_model(self):
        return get_model(self.env)

    def _extract_json(self, content):
        if not content:
            return {}
        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content)
        raw = fenced.group(1) if fenced else None
        if not raw:
            m = re.search(r"\{[\s\S]*\}", content)
            raw = m.group(0) if m else None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _safe_float(self, val):
        try:
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    def _parse_date(self, date_str):
        if not date_str or str(date_str).lower() in ("null", "none", ""):
            return None
        try:
            parts = str(date_str).split("-")
            from datetime import date
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return None

    # ── Vendor / account lookup ───────────────────────────────────────────────

    def _normalize(self, name):
        """Lowercase, remove punctuation, collapse whitespace."""
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()

    def _name_similarity(self, a, b):
        """Word-overlap ratio (Jaccard-like) between two strings."""
        wa = set(self._normalize(a).split())
        wb = set(self._normalize(b).split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def _match_vendor(self, vendor_name):
        """
        Fuzzy-match the AI-extracted vendor name to an existing res.partner.
        Tries: exact → ML knowledge → keyword search.
        """
        if not vendor_name:
            return None
        norm = self._normalize(vendor_name)

        # 1. Exact case-insensitive match
        p = self.env["res.partner"].search(
            [("is_company", "=", True), ("name", "=ilike", vendor_name)], limit=1
        )
        if p:
            return p

        # 2. Check ML knowledge for a previously resolved name→partner mapping
        kb = self.env["premafirm.ml.knowledge"].search(
            [("knowledge_type", "=", "bill_import")],
            order="weight desc, create_date desc",
            limit=30,
        )
        best_partner = None
        best_score = 0.0
        for entry in kb:
            try:
                data = json.loads(entry.good_output or "{}")
                known_name = data.get("partner_name") or ""
                if not known_name or not data.get("partner_id"):
                    continue
                score = self._name_similarity(vendor_name, known_name)
                if score > best_score and score >= 0.4:
                    partner = self.env["res.partner"].browse(data["partner_id"]).exists()
                    if partner:
                        best_score = score
                        best_partner = partner
            except Exception:
                pass
        if best_partner:
            _logger.info("Bill scan vendor match via ML: '%s' → '%s' (score=%.2f)",
                         vendor_name, best_partner.name, best_score)
            return best_partner

        # 3. Keyword search — every meaningful word in the name
        words = [w for w in norm.split() if len(w) > 3][:4]
        for word in words:
            p = self.env["res.partner"].search([
                ("is_company", "=", True),
                ("name", "ilike", word),
                ("supplier_rank", ">", 0),
            ], limit=1)
            if p:
                return p

        return None

    def _get_best_account_from_ml(self, vendor_name, partner, company_id):
        """
        Look at past bill patterns in the ML knowledge base for this vendor and
        return the most-used expense account.  Falls back to the general default.
        """
        # Build candidate search strings
        search_terms = [vendor_name or ""]
        if partner:
            search_terms.append(partner.name)

        account_votes = Counter()
        for term in search_terms:
            if not term:
                continue
            kb = self.env["premafirm.ml.knowledge"].search(
                [("knowledge_type", "=", "bill_import")],
                order="weight desc, create_date desc",
                limit=50,
            )
            for entry in kb:
                score = self._name_similarity(term, entry.input_context)
                if score < 0.2:
                    continue
                try:
                    data = json.loads(entry.good_output or "{}")
                    codes = data.get("account_codes") or []
                    for code in codes:
                        account_votes[code] += max(1, int(score * 10))
                except Exception:
                    pass

        if account_votes:
            top_code = account_votes.most_common(1)[0][0]
            acc = self.env["account.account"].search([
                ("code", "=", top_code),
                ("company_ids", "in", [company_id]),
                ("deprecated", "=", False),
            ], limit=1)
            if acc:
                _logger.info("Bill scan ML account for '%s': %s %s", vendor_name, acc.code, acc.name)
                return acc

        # Fallback: first expense account that is NOT Labour/Salary/Wage
        acc = self.env["account.account"].search([
            ("company_ids", "in", [company_id]),
            ("account_type", "=", "expense"),
            ("deprecated", "=", False),
            ("name", "not ilike", "labour"),
            ("name", "not ilike", "salary"),
            ("name", "not ilike", "wage"),
            ("name", "not ilike", "commission"),
            ("name", "not ilike", "benefit"),
        ], order="code asc", limit=1)
        if acc:
            return acc

        # Last resort: truly any expense account
        return self.env["account.account"].search([
            ("company_ids", "in", [company_id]),
            ("account_type", "=", "expense"),
            ("deprecated", "=", False),
        ], order="code asc", limit=1)

    def _get_purchase_journal(self, company):
        j = self.env["account.journal"].search([
            ("type", "=", "purchase"),
            ("company_id", "=", company.id),
            ("name", "not ilike", "USD"),
        ], limit=1)
        return j or self.env["account.journal"].search([
            ("type", "=", "purchase"),
            ("company_id", "=", company.id),
        ], limit=1)

    # ── ML knowledge helpers ──────────────────────────────────────────────────

    def _get_ml_context(self, vendor_hint=""):
        try:
            query = f"vendor invoice bill {vendor_hint}"
            examples = self.env["premafirm.ml.knowledge"]._search_similar(
                "bill_import", query, limit=4
            )
            if not examples:
                return ""
            lines = ["\n\nPAST BILL PATTERNS (for vendor/account guidance):"]
            for ex in examples:
                lines.append(
                    f"- Context: {(ex.input_context or '')[:200]}\n"
                    f"  Pattern: {(ex.good_output or '')[:300]}"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    def _save_to_ml(self, vendor_name, invoice_number, data, bill):
        """
        Save bill pattern to ML ONLY after the bill has been successfully created
        with correct accounts (as verified by the partner match + ML account lookup).
        Does NOT save if no partner was matched (accounts would be wrong defaults).
        """
        try:
            if not bill.partner_id:
                return  # Don't persist patterns where we couldn't identify the vendor
            accounts = [
                f"{ln.account_id.code}:{ln.name[:30]}"
                for ln in bill.invoice_line_ids if ln.account_id
            ]
            input_ctx = (
                f"Vendor: {vendor_name} | "
                f"Invoice: {invoice_number} | "
                f"Total: {data.get('total', 0)} {data.get('currency', 'CAD')} | "
                f"Accounts: {', '.join(a.split(':')[0] for a in accounts)}"
            )
            good_output = json.dumps({
                "vendor_name":    vendor_name,
                "partner_id":     bill.partner_id.id,
                "partner_name":   bill.partner_id.name,
                "invoice_number": invoice_number,
                "invoice_date":   str(data.get("invoice_date", "")),
                "total":          data.get("total"),
                "currency":       data.get("currency", "CAD"),
                "account_codes":  [ln.account_id.code for ln in bill.invoice_line_ids if ln.account_id],
                "lines":          [
                    {"description": ln.name, "account_code": ln.account_id.code}
                    for ln in bill.invoice_line_ids if ln.account_id
                ],
            }, indent=2)
            self.env["premafirm.ml.knowledge"].sudo().create({
                "knowledge_type": "bill_import",
                "input_context":  input_ctx,
                "good_output":    good_output,
                "origin":         "approved",
                "weight":         1.2,
            })
        except Exception:
            _logger.exception("Failed to save bill pattern to ML knowledge")

    # ── DeepSeek text-only call (OCR first) ──────────────────────────────────

    def _ocr_image(self, image_b64):
        """OCR a base64-encoded image, returning extracted text or empty string."""
        import base64 as _b64
        import io as _io
        try:
            from PIL import Image
            import pytesseract
            img_bytes = _b64.b64decode(image_b64)
            img = Image.open(_io.BytesIO(img_bytes))
            text = pytesseract.image_to_string(img, lang="eng")
            return (text or "").strip()
        except Exception:
            _logger.exception("Bill scan OCR failed")
            return ""

    def _send_to_deepseek(self, image_b64, mimetype):
        api_key = self._get_api_key()
        model = self._get_model()
        ml_context = self._get_ml_context()

        # OCR the image first — DeepSeek chat models are text-only
        ocr_text = self._ocr_image(image_b64)
        if not ocr_text:
            raise RuntimeError(
                "Could not extract text from this bill image. "
                "The image may be too blurry or low-resolution."
            )

        prompt = (
            "You are a bill/invoice data extraction specialist for PremaFirm Inc., "
            "a Canadian trucking company.\n"
            "Below is OCR-extracted text from a scanned bill. "
            "Extract ALL visible data from the text.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "vendor_name": "Exact company name on the bill",\n'
            '  "vendor_address": "Full vendor address or null",\n'
            '  "invoice_number": "Invoice or receipt number or null",\n'
            '  "invoice_date": "YYYY-MM-DD or null",\n'
            '  "due_date": "YYYY-MM-DD or null",\n'
            '  "currency": "CAD or USD",\n'
            '  "lines": [\n'
            '    {"description": "Item or service", "quantity": 1.0, '
            '"unit_price": 0.00, "amount": 0.00}\n'
            '  ],\n'
            '  "subtotal": 0.00,\n'
            '  "tax_amount": 0.00,\n'
            '  "tax_label": "HST 13% or GST 5% or null",\n'
            '  "total": 0.00,\n'
            '  "notes": "Payment terms or null"\n'
            "}\n\n"
            "Rules: amounts are plain numbers, dates are YYYY-MM-DD, "
            "extract every line item, unknown fields → null\n\n"
            "OCR TEXT FROM SCANNED BILL:\n" + ocr_text
            + ml_context
        )

        try:
            raw = deepseek_chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                api_key=api_key,
                model=model,
                timeout=120,
            )
            _logger.info("Bill scan DeepSeek response: %s", raw[:600])
            return raw, self._extract_json(raw)
        except Exception as exc:
            raise RuntimeError(f"DeepSeek bill scan failed: {exc}") from exc

    # ── Folder scan (discover only — no processing) ───────────────────────────

    def scan_folder_only(self):
        """
        Read all unprocessed files from 'bills-entry' and create pending import
        records for them.  Does NOT call Claude — just prepares the queue.
        Returns the count of new records created.
        """
        folder = self.env["documents.document"].search(
            [("type", "=", "folder"), ("name", "=", BILLS_ENTRY_FOLDER)], limit=1
        )
        if not folder:
            raise ValueError(f"Folder '{BILLS_ENTRY_FOLDER}' not found in Odoo Documents.")

        docs = self.env["documents.document"].search([
            ("folder_id", "=", folder.id),
            ("type", "!=", "folder"),
        ])

        # Already have import records for these docs
        existing = {
            r.document_id.id: r
            for r in self.env["premafirm.bill.scan.import"].search([
                ("document_id", "in", docs.ids),
            ])
        }

        new_count = 0
        for doc in docs:
            if doc.id not in existing:
                self.env["premafirm.bill.scan.import"].sudo().create({
                    "document_id": doc.id,
                    "state":       "pending",
                })
                new_count += 1

        return new_count

    # ── Single record processing ──────────────────────────────────────────────

    def process_import_record(self, imp):
        """Process one premafirm.bill.scan.import record end-to-end."""
        from odoo import fields as F

        doc = imp.document_id
        if not doc or not doc.attachment_id:
            imp.write({"state": "error", "error_message": "Document has no attachment."})
            return

        try:
            att = doc.attachment_id
            if not att.datas:
                imp.write({"state": "error", "error_message": "Attachment data is empty."})
                return

            image_b64 = att.datas.decode() if isinstance(att.datas, bytes) else att.datas
            mimetype  = att.mimetype or "image/jpeg"

            # ── Claude Vision ──────────────────────────────────────────────────
            raw_json, data = self._send_to_deepseek(image_b64, mimetype)
            if not data:
                imp.write({
                    "state": "error",
                    "error_message": "AI returned no structured data.",
                    "ai_raw_json": raw_json,
                })
                return

            vendor_name    = data.get("vendor_name") or ""
            invoice_number = data.get("invoice_number") or ""
            invoice_date   = self._parse_date(data.get("invoice_date"))
            due_date       = self._parse_date(data.get("due_date"))
            total          = self._safe_float(data.get("total"))
            subtotal       = self._safe_float(data.get("subtotal"))
            tax_amount     = self._safe_float(data.get("tax_amount"))
            currency_code  = (data.get("currency") or "CAD").strip().upper()
            lines          = data.get("lines") or []

            imp.write({
                "ai_vendor_name":    vendor_name,
                "ai_invoice_number": invoice_number,
                "ai_invoice_date":   invoice_date,
                "ai_due_date":       due_date,
                "ai_total":          total,
                "ai_subtotal":       subtotal,
                "ai_tax_amount":     tax_amount,
                "ai_currency":       currency_code,
                "ai_raw_json":       raw_json,
            })

            # ── Vendor + ML account lookup ─────────────────────────────────────
            company  = self.env.company
            partner  = self._match_vendor(vendor_name)
            account  = self._get_best_account_from_ml(vendor_name, partner, company.id)
            journal  = self._get_purchase_journal(company)

            # ── Invoice lines ──────────────────────────────────────────────────
            move_lines = []
            if lines:
                for ln in lines:
                    move_lines.append((0, 0, {
                        "name":       ln.get("description") or "Service",
                        "quantity":   self._safe_float(ln.get("quantity")) or 1.0,
                        "price_unit": self._safe_float(ln.get("unit_price") or ln.get("amount")),
                        "account_id": account.id if account else False,
                    }))
            else:
                move_lines.append((0, 0, {
                    "name":       f"Bill from {vendor_name}" if vendor_name else "Vendor Bill",
                    "quantity":   1.0,
                    "price_unit": subtotal or total,
                    "account_id": account.id if account else False,
                }))

            # ── Create draft bill ──────────────────────────────────────────────
            bill = self.env["account.move"].sudo().create({
                "move_type":        "in_invoice",
                "partner_id":       partner.id if partner else False,
                "ref":              invoice_number or False,
                "invoice_date":     invoice_date or False,
                "invoice_date_due": due_date or False,
                "journal_id":       journal.id if journal else False,
                "invoice_line_ids": move_lines,
                "narration":        data.get("notes") or False,
            })

            # ── Move attachment to the bill (true move, not copy) ─────────────
            # Re-point the existing ir.attachment so it appears in the bill's
            # Chatter without duplicating the file data in the database.
            existing_att = doc.attachment_id
            if existing_att:
                existing_att.sudo().write({
                    "res_model": "account.move",
                    "res_id":    bill.id,
                })
            else:
                self.env["ir.attachment"].sudo().create({
                    "name":      doc.name,
                    "datas":     image_b64,
                    "res_model": "account.move",
                    "res_id":    bill.id,
                    "mimetype":  mimetype,
                })

            # ── Move document → Finance folder, link to bill ───────────────────
            finance = self.env["documents.document"].search(
                [("type", "=", "folder"), ("name", "=", "Finance")], limit=1
            )
            doc.sudo().write({
                "res_model": "account.move",
                "res_id":    bill.id,
                "folder_id": finance.id if finance else doc.folder_id.id,
            })

            # ── Save to ML only when vendor was identified (accounts are reliable)
            self._save_to_ml(vendor_name, invoice_number, data, bill)

            imp.write({
                "state":        "done",
                "bill_id":      bill.id,
                "processed_at": F.Datetime.now(),
            })
            _logger.info("Bill scan: created %s from '%s' (partner=%s, account=%s)",
                         bill.name, doc.name,
                         partner.name if partner else "?",
                         account.code if account else "?")

        except Exception as exc:
            _logger.exception("Bill scan failed for document %s", doc.id)
            imp.write({"state": "error", "error_message": str(exc)[:1000]})
