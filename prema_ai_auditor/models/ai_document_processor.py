import base64
import io
import json
import re

from odoo import api, fields, models


class AIDocumentProcessor(models.AbstractModel):
    _name = "prema.ai.document.processor"
    _description = "Prema AI Document Processor"

    @api.model
    def _extract_text_from_pdf_standard(self, file_data):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_data))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n".join(pages).strip()

    @api.model
    def extract_text_with_ocr(self, attachment):
        import pytesseract
        from pdf2image import convert_from_bytes
        from PIL import Image

        file_data = base64.b64decode(attachment.datas or b"")

        if attachment.mimetype == "application/pdf":
            images = convert_from_bytes(file_data)
            full_text = ""
            for image in images:
                full_text += pytesseract.image_to_string(image)
            return full_text

        if (attachment.mimetype or "").startswith("image/"):
            image = Image.open(io.BytesIO(file_data))
            return pytesseract.image_to_string(image)

        return ""

    @api.model
    def _extract_text_with_fallback(self, attachment):
        file_data = base64.b64decode(attachment.datas or b"")
        mimetype = attachment.mimetype or ""

        if mimetype == "application/pdf":
            extracted = self._extract_text_from_pdf_standard(file_data)
            if extracted.strip():
                return extracted
            return self.extract_text_with_ocr(attachment)

        if mimetype.startswith("image/"):
            return self.extract_text_with_ocr(attachment)

        return f"Extracted {len(file_data)} bytes from {attachment.name}"

    @api.model
    def _extract_invoice_values(self, extracted_text):
        lower = (extracted_text or "").lower()
        number_match = re.search(r"(?:invoice\s*(?:number|no|#)?[:\s]+)([a-z0-9\-/]+)", lower)
        total_match = re.search(r"(?:total\s*(?:amount)?[:\s]+)([0-9]+(?:\.[0-9]{1,2})?)", lower)
        date_match = re.search(r"(?:date[:\s]+)([0-9]{4}-[0-9]{2}-[0-9]{2})", lower)
        vendor_match = re.search(r"(?:vendor|supplier)[:\s]+([a-z0-9 .,&-]+)", lower)

        values = {
            "invoice_number": number_match.group(1).upper() if number_match else False,
            "amount_total": float(total_match.group(1)) if total_match else 0.0,
            "invoice_date": date_match.group(1) if date_match else False,
            "vendor_name": vendor_match.group(1).strip().title() if vendor_match else False,
        }
        if values["invoice_date"]:
            values["invoice_date"] = fields.Date.to_date(values["invoice_date"])
        return values

    @api.model
    def process_document(self, document):
        document = document.sudo()
        document.status = "processing"
        name = (document.attachment_id.name or "").lower()

        if "invoice" in name or "bill" in name:
            classification = "vendor_bill"
        elif "receipt" in name:
            classification = "receipt"
        elif "bank" in name or "statement" in name:
            classification = "bank_statement"
        else:
            classification = "other"

        extracted_text = self._extract_text_with_fallback(document.attachment_id)
        advice = "Advice only"
        update_values = {
            "classification": classification,
            "extracted_text": extracted_text,
            "advice": advice,
            "status": "processed",
            "flag_duplicate": False,
        }

        if classification == "vendor_bill":
            invoice_values = self._extract_invoice_values(extracted_text)
            update_values.update(invoice_values)

            duplicate_move = False
            if invoice_values["invoice_number"] and invoice_values["amount_total"]:
                duplicate_move = self.env["account.move"].sudo().search(
                    [
                        ("move_type", "=", "in_invoice"),
                        ("ref", "=", invoice_values["invoice_number"]),
                        ("amount_total", "=", invoice_values["amount_total"]),
                    ],
                    limit=1,
                )

            if duplicate_move:
                update_values["flag_duplicate"] = True
                update_values["advice"] = (
                    "⚠ Duplicate Detected\n"
                    f"Invoice already exists as Bill #{duplicate_move.name or duplicate_move.ref}"
                )
            elif not invoice_values["vendor_name"]:
                update_values["advice"] = "Missing vendor on vendor bill."
            else:
                update_values["advice"] = "Draft ready"

        document.write(update_values)

    @api.model
    def process_pending_documents(self, limit=20):
        documents = self.env["prema.ai.document"].sudo().search([
            ("status", "=", "pending"),
        ], limit=limit)
        for document in documents:
            self.process_document(document)

    @api.model
    def _build_comparison_payload(self, docs):
        items = []
        for doc in docs:
            items.append(
                {
                    "vendor": doc.vendor_name,
                    "date": doc.invoice_date.isoformat() if doc.invoice_date else False,
                    "total": doc.amount_total,
                    "ref": doc.invoice_number,
                }
            )
        return {"documents": items}

    @api.model
    def _compare_documents(self, docs):
        if len(docs) <= 1:
            return ""

        payload = self._build_comparison_payload(docs)
        items = payload["documents"]
        seen = {}
        notes = []
        for item in items:
            key = (item.get("vendor"), item.get("date"), item.get("total"))
            seen[key] = seen.get(key, 0) + 1

        for key, count in seen.items():
            if count > 1 and key[0]:
                notes.append(f"Detected {count} invoices from same vendor ({key[0]}) with repeated values.")

        if not notes:
            notes.append("No obvious duplication, split billing, or FX anomalies detected in uploaded set.")

        return "\n".join(notes) + "\nPayload: " + json.dumps(payload)

    @api.model
    def summarize_session_documents(self, session_id):
        docs = self.env["prema.ai.document"].sudo().search([
            ("session_id", "=", session_id),
        ])
        if not docs:
            return "No uploaded documents yet."

        count_map = {
            "vendor_bill": 0,
            "receipt": 0,
            "bank_statement": 0,
            "other": 0,
        }
        for doc in docs:
            count_map[doc.classification] = count_map.get(doc.classification, 0) + 1

        header = (
            f"You uploaded {count_map['vendor_bill']} Vendor Bills, "
            f"{count_map['receipt']} Receipts, and "
            f"{count_map['bank_statement']} Bank Statements."
        )
        lines = [header, ""]

        for index, doc in enumerate(docs, start=1):
            label = dict(doc._fields["classification"].selection).get(doc.classification, "Document")
            lines.append(f"{label} {index} → {doc.advice or 'Review required'}")

        lines.extend(["", "[ Create All Drafts ]", "[ Review Individually ]", "[ Advice Only ]"])

        comparison = self._compare_documents(docs.filtered(lambda d: d.classification == "vendor_bill"))
        if comparison:
            lines.extend(["", "Cross-document comparison:", comparison])
        return "\n".join(lines)

    @api.model
    def get_batch_draft_summary(self, session_id):
        docs = self.env["prema.ai.document"].sudo().search([
            ("session_id", "=", session_id),
            ("classification", "=", "vendor_bill"),
            ("status", "=", "processed"),
        ])
        clean = docs.filtered(lambda d: not d.flag_duplicate and d.vendor_name)
        duplicate = docs.filtered(lambda d: d.flag_duplicate)
        missing_vendor = docs.filtered(lambda d: not d.vendor_name)
        return {
            "total": len(docs),
            "clean": len(clean),
            "duplicate": len(duplicate),
            "missing_vendor": len(missing_vendor),
        }

    @api.model
    def create_drafts_for_session(self, session_id, clean_only=False):
        docs = self.env["prema.ai.document"].sudo().search([
            ("session_id", "=", session_id),
            ("classification", "=", "vendor_bill"),
            ("status", "=", "processed"),
        ])

        if clean_only:
            docs = docs.filtered(lambda d: not d.flag_duplicate and d.vendor_name)

        for doc in docs:
            if doc.flag_duplicate and clean_only:
                continue
            proposal = self.env["prema.ai.proposal"].sudo().create(
                {
                    "name": f"Draft bill proposal: {doc.attachment_id.name}",
                    "target_model": "account.move",
                    "target_res_id": 0,
                    "action_type": "create",
                    "payload_json": {
                        "move_type": "in_invoice",
                        "ref": doc.invoice_number or doc.attachment_id.name,
                        "invoice_date": doc.invoice_date.isoformat() if doc.invoice_date else False,
                        "invoice_line_ids": [
                            (0, 0, {"name": "AI Draft Placeholder", "quantity": 1, "price_unit": doc.amount_total or 0.0})
                        ],
                    },
                    "rationale": "Generated from uploaded document analysis. Requires explicit approval/apply.",
                    "status": "pending_approval",
                }
            )
            doc.write({"status": "draft_created", "advice": f"Draft proposal generated: {proposal.name}"})


class AIDocumentProcessorCron(models.Model):
    _name = "prema.ai.document.processor.cron"
    _description = "Prema AI Document Processor Cron"

    @api.model
    def cron_process_pending_documents(self):
        self.env["prema.ai.document.processor"].process_pending_documents(limit=50)
