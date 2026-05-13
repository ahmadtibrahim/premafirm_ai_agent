import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile

import requests
from .openai_utils import openai_chat as _claude_chat, DEFAULT_MODEL as _CLAUDE_DEFAULT

_logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"  # kept for reference
MAX_IMAGE_PARTS = 5  # cap to keep token usage reasonable

_PROVINCE_MAP = {
    "ontario": "ON",
    "quebec": "QC",
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland & labrador": "NL",
    "nova scotia": "NS",
    "prince edward island": "PE",
    "saskatchewan": "SK",
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
}

# Regex patterns for zero-cost reference extraction from PDF text
_REF_PATTERNS = [
    (re.compile(r'(?:packing\s+slip\s*(?:number|no\.?|#)?|slip\s*#|ps\s*#|ps\s*no\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})', re.I), 'PS'),
    (re.compile(r'(?:bill\s+of\s+lading\s*(?:no\.?|#)?|bol\s*#?|b/l\s*#?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})', re.I), 'BOL'),
    (re.compile(r'(?:delivery\s*(?:note|#|no\.?)|del\.?\s*#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})', re.I), 'DEL'),
    (re.compile(r'(?:p\.?\s*o\.?\s*(?:number|no\.?|#)|purchase\s+order\s*(?:no\.?|#)?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/ ]{2,30})', re.I), 'PO'),
    (re.compile(r'(?:reference\s*(?:no\.?|#)?|ref\.?\s*#|booking\s*#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})', re.I), 'REF'),
]


class InvoiceAIService:
    """Analyzes invoice attachments with GPT-4o vision and generates reference + description."""

    def __init__(self, env):
        self.env = env

    def _get_openai_key(self):
        p = self.env["ir.config_parameter"].sudo()
        return p.get_param("openai.api_key") or p.get_param("prema_ai.api_key") or ""

    def _get_model(self):
        p = self.env["ir.config_parameter"].sudo()
        return p.get_param("prema_ai.fast_model") or _CLAUDE_DEFAULT

    def _get_attachment_bytes(self, attachment):
        """Read attachment bytes directly from filestore when possible."""
        if getattr(attachment, "store_fname", None):
            try:
                data = attachment._file_read(attachment.store_fname)
                if data:
                    return data
            except Exception:
                _logger.exception("Failed to read filestore data for %s", attachment.name)
        if getattr(attachment, "raw", None):
            return attachment.raw
        if attachment.datas:
            try:
                return base64.b64decode(attachment.datas)
            except Exception:
                _logger.exception("Failed to decode attachment datas for %s", attachment.name)
        return b""

    def _extract_json_from_text(self, content):
        if not content:
            return {}
        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content)
        raw_json = fenced.group(1) if fenced else None
        if not raw_json:
            match = re.search(r"\{[\s\S]*\}", content)
            raw_json = match.group(0) if match else None
        if not raw_json:
            return {}
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            _logger.exception("JSON parse failed")
            return {}

    def _pdf_extract_text(self, pdf_bytes):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = [page.extract_text() or "" for page in pdf.pages]
            text = "\n".join(pages_text).strip()
            if text:
                return text
        except Exception:
            _logger.exception("pdfplumber extraction failed")
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ["pdftotext", tmp_path, "-"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return (result.stdout or "").strip()
        except Exception:
            _logger.exception("pdftotext extraction failed")
            return ""
        finally:
            try:
                if 'tmp_path' in locals() and tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                _logger.exception("Failed to remove temp PDF %s", tmp_path)

    def _pdf_to_images_b64(self, pdf_bytes):
        """Convert PDF pages to base64 JPEGs using pdf2image."""
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(pdf_bytes, dpi=100, fmt="jpeg")
            result = []
            for img in images[:4]:  # max 4 pages per PDF
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                result.append(base64.b64encode(buf.getvalue()).decode())
            return result
        except Exception:
            _logger.exception("PDF to image conversion failed")
            return []

    def _build_content_parts(self, invoice):
        """Build the GPT-4o content array from all invoice attachments."""
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])

        content_parts = []
        image_mimes = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }

        for att in attachments:
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes or len(content_parts) >= MAX_IMAGE_PARTS:
                break

            name = (att.name or "").lower()

            # Direct image files
            matched_mime = next(
                (mime for ext, mime in image_mimes.items() if name.endswith(ext)),
                None,
            )
            if matched_mime:
                b64 = base64.b64encode(file_bytes).decode()
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{matched_mime};base64,{b64}", "detail": "low"},
                })
                continue

            # PDF: try text extraction first, fall back to vision
            if name.endswith(".pdf"):
                text = self._pdf_extract_text(file_bytes)
                if text and len(text) > 80:
                    content_parts.append({"type": "text", "text": f"[Document: {att.name}]\n{text}"})
                else:
                    # Scanned PDF — render pages as images
                    for b64 in self._pdf_to_images_b64(file_bytes):
                        if len(content_parts) >= MAX_IMAGE_PARTS:
                            break
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                        })

        return content_parts

    def _collect_attachment_text(self, invoice):
        """Collect text from readable PDF attachments for low-cost heuristics."""
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        text_parts = []
        for att in attachments:
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes or not (att.name or "").lower().endswith(".pdf"):
                continue
            text = self._pdf_extract_text(file_bytes)
            if text:
                text_parts.append(text)
        return "\n\n".join(text_parts).strip()

    def _normalize_region(self, region):
        region = (region or "").strip()
        if not region:
            return ""
        upper = region.upper()
        if len(upper) == 2 and upper.isalpha():
            return upper
        return _PROVINCE_MAP.get(region.lower(), region)

    def _extract_weekly_schedule_details(self, text):
        """
        Detect weekly rate confirmations that do not list explicit destination cities.
        Returns heuristic values that are safer than hallucinating a route.
        """
        if not text:
            return {}

        lower = text.lower()
        if "rate confirmation" not in lower or "total week" not in lower:
            return {}

        week_match = re.search(r"Week:\s*([^\n]+)", text, re.I)
        ref_match = re.search(r"Ref\s*#:\s*([A-Z0-9\-]+)", text, re.I)
        pallets_match = re.search(r"Pallet Count.*?(\d+)\s*Pallets", text, re.I | re.S)
        address_match = re.search(
            r"Pickup Address\s+([^\n]+?,\s*[A-Za-z .'-]+,\s*[A-Za-z .'-]+)",
            text,
            re.I,
        )
        location_match = re.search(
            r"Pickup Address\s+[^\n]*,\s*([A-Za-z .'-]+),\s*([A-Za-z .'-]+)",
            text,
            re.I,
        )
        money_values = re.findall(r"\$([\d,]+\.\d{2})", text)

        amount = None
        if money_values:
            try:
                amount = float(money_values[-1].replace(",", ""))
            except ValueError:
                amount = None

        origin = None
        if location_match:
            city = (location_match.group(1) or "").strip()
            region = self._normalize_region(location_match.group(2))
            if city and region:
                origin = f"{city}, {region}"
            elif city:
                origin = city
        elif address_match:
            origin = address_match.group(1).strip()

        week_value = (week_match.group(1).strip() if week_match else "")
        pallets = (pallets_match.group(1).strip() if pallets_match else "")

        description_lines = ["Freight / Delivery Service"]
        if week_value:
            description_lines.append(f"Schedule: {week_value}")
        if origin:
            description_lines.append(f"Origin: {origin}")

        service_bits = []
        if "delivery route + pickup" in lower:
            service_bits.append("Daily delivery route plus pickup")
        elif "pickup only" in lower:
            service_bits.append("Pickup-only service")
        if pallets:
            service_bits.append(f"{pallets} pallets/day")
        if service_bits:
            description_lines.append(f"Service: {' | '.join(service_bits)}")

        details = {
            "description": "\n".join(description_lines),
            "amount": amount,
            "line_items": self._extract_weekly_schedule_line_items(text),
        }
        if ref_match:
            details["reference"] = f"REF-{ref_match.group(1).strip()}"
        return details

    def _extract_weekly_schedule_line_items(self, text):
        """Extract dated service rows from weekly rate confirmations."""
        if not text:
            return []

        normalized = re.sub(r"\s+", " ", text.replace("—", "-").replace("–", "-")).strip()
        if "WEEKLY SCHEDULE" not in text and "TOTAL WEEK" not in normalized:
            return []

        day_names = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
        date_pattern = re.compile(rf"((?:{day_names}),\s+[A-Za-z]+\s+\d{{2}},\s+\d{{4}})", re.I)
        known_activities = (
            "Pickup only - No delivery",
            "Delivery Route + Pickup",
            "Delivery Route - Final Day",
        )
        rows = []
        parts = date_pattern.split(normalized)
        for idx in range(1, len(parts), 2):
            date_text = parts[idx].strip()
            tail = parts[idx + 1] if idx + 1 < len(parts) else ""
            tail = tail.split("TOTAL WEEK", 1)[0].strip()
            if not tail:
                continue
            activity = next((label for label in known_activities if label in tail), None)
            if not activity:
                continue
            amounts = re.findall(r"\$([\d,]+\.\d{2})", tail)
            amount = None
            if amounts:
                try:
                    amount = float(amounts[0].replace(",", ""))
                except ValueError:
                    amount = None
            rows.append({
                "name": f"{date_text} - {activity}",
                "amount": amount,
            })

        if not rows:
            return []

        total_week_split = re.split(r"TOTAL WEEK", normalized, maxsplit=1, flags=re.I)
        tail_amounts = []
        if len(total_week_split) == 2:
            tail_amounts = re.findall(r"\$([\d,]+\.\d{2})", total_week_split[1])
            parsed_tail = []
            for value in tail_amounts:
                try:
                    parsed_tail.append(float(value.replace(",", "")))
                except ValueError:
                    continue
            tail_amounts = parsed_tail

        missing_rows = [row for row in rows if row["amount"] is None]
        if missing_rows and tail_amounts:
            inferred = tail_amounts[:-1] if len(tail_amounts) > len(missing_rows) else tail_amounts
            for row, value in zip(missing_rows, inferred):
                row["amount"] = value

        return [row for row in rows if row.get("amount") is not None]

    def _extract_references_from_text(self, text):
        """
        Zero-cost regex extraction of reference numbers from plain text.
        Returns a formatted reference string (e.g. 'PS-5000087157 | PO-689') or None.
        Called before making any API call to save credits.
        """
        if not text or len(text) < 10:
            return None

        found = {}  # prefix → list of values
        for pattern, prefix in _REF_PATTERNS:
            for m in pattern.finditer(text):
                val = m.group(1).strip().rstrip('.,;')
                # Skip values that look like phone numbers, postal codes, or prices
                if re.match(r'^\d{3}[\s\-]\d{3}[\s\-]\d{4}$', val):
                    continue
                if re.match(r'^[A-Z]\d[A-Z]\s?\d[A-Z]\d$', val, re.I):
                    continue
                found.setdefault(prefix, [])
                if val not in found[prefix]:
                    found[prefix].append(val)

        if not found:
            return None

        parts = []
        for prefix in ('PS', 'BOL', 'DEL', 'PO', 'REF'):
            if prefix not in found:
                continue
            vals = found[prefix]
            if len(vals) == 1:
                parts.append(f"{prefix}-{vals[0]}")
            else:
                parts.append(f"{prefix}-{vals[0]}, " + ", ".join(vals[1:]))

        result = " | ".join(parts)
        _logger.info("Zero-cost regex reference extraction: %s", result)
        return result or None

    def _get_ai_products_text(self):
        """Return a formatted list of AI-enabled products for the prompt."""
        products = self.env["premafirm.invoice.ai.product"].search([("ai_enabled", "=", True)])
        if not products:
            return "No products configured — do not populate product_id."
        lines = []
        for p in products:
            hint = f" | Hint: {p.description_hint}" if p.description_hint else ""
            lines.append(
                f'  id={p.product_id.id} service_type="{p.service_type}" name="{p.product_id.name}"{hint}'
            )
        return "\n".join(lines)

    def _build_input_context(self, invoice):
        """Build a searchable context string from the invoice for ML storage/lookup."""
        parts = [f"Partner: {invoice.partner_id.name or ''}"]
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        if attachments:
            names = [a.name for a in attachments if a.name]
            parts.append(f"Attachments: {', '.join(names[:5])}")
        if invoice.invoice_date:
            parts.append(f"Date: {invoice.invoice_date}")
        return " | ".join(parts)

    def _get_ml_examples(self, context_query):
        """Fetch relevant past invoice examples from the ML knowledge base."""
        try:
            examples = self.env["premafirm.ml.knowledge"]._search_similar(
                "invoice_flag", context_query, limit=4
            )
            if not examples:
                return ""
            lines = ["══ PAST EXAMPLES — use these as guidance for format and style ══"]
            for ex in examples:
                note = f"\n  Staff correction: {ex.correction_note}" if ex.correction_note else ""
                lines.append(
                    f"Context: {(ex.input_context or '')[:200]}\n"
                    f"Output: {(ex.good_output or '')[:400]}{note}"
                )
            return "\n\n".join(lines)
        except Exception:
            _logger.exception("ML example fetch failed — proceeding without examples")
            return ""

    def save_to_ml(self, invoice, result):
        """
        Save this AI generation result to the ML knowledge base.
        Returns the created knowledge record (or None on failure).
        """
        try:
            input_context = self._build_input_context(invoice)
            good_output = json.dumps({
                "reference":    result.get("reference", ""),
                "description":  result.get("description", ""),
                "service_type": result.get("service_type", ""),
            }, indent=2)
            record = self.env["premafirm.ml.knowledge"].sudo().create({
                "knowledge_type": "invoice_flag",
                "input_context":  input_context,
                "good_output":    good_output,
                "origin":         "approved",
                "weight":         1.0,
            })
            _logger.info("Invoice AI result saved to ML knowledge #%s", record.id)
            return record
        except Exception:
            _logger.exception("Failed to save invoice AI result to ML")
            return None

    def extract_reference_only(self, invoice):
        """
        Extract reference numbers from attachments.
        First tries zero-cost regex on PDF text; only calls GPT for scanned/image attachments.
        Returns a formatted reference string like 'BOL-12345 | PO-6789', or None.
        """
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        if not attachments:
            raise ValueError("No readable attachments found on this invoice.")

        # Zero-cost path: try regex on extracted PDF text first
        all_text_parts = []
        has_image_only = False
        for att in attachments:
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes:
                continue
            name = (att.name or "").lower()
            if name.endswith(".pdf"):
                text = self._pdf_extract_text(file_bytes)
                if text and len(text) > 80:
                    all_text_parts.append(text)
                else:
                    has_image_only = True
            elif any(name.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp')):
                has_image_only = True

        if all_text_parts:
            combined_text = "\n\n".join(all_text_parts)
            ref = self._extract_references_from_text(combined_text)
            if ref:
                return ref
            # Text extracted but regex found nothing — fall through to GPT only if no images
            if not has_image_only:
                _logger.info("Regex found nothing in PDF text for %s; no images to scan", invoice.name)
                return None

        # GPT fallback: needed for scanned PDFs or pure image attachments
        api_key = self._get_openai_key()
        if not api_key:
            raise ValueError("OpenAI API key not configured in Settings.")

        content_parts = self._build_content_parts(invoice)
        if not content_parts:
            raise ValueError("No readable attachments found on this invoice.")

        user_content = [{"type": "text", "text": (
            "You are reading shipping/delivery documents (Packing Slips, BOLs, Delivery Notes, etc.).\n"
            "Extract ALL document/reference numbers and return ONE clean reference string.\n\n"

            "STEP 1 — Find numbers from these fields (scan every page):\n"
            "  • Packing Slip NUMBER / Slip # / PS # → prefix each with PS-\n"
            "  • BOL # / Bill of Lading # → prefix each with BOL-\n"
            "  • Delivery # / Delivery Note # → prefix each with DEL-\n"
            "  • P.O. NUMBER / Purchase Order # → prefix each with PO-\n"
            "  • Reference # / Booking # / REF # → prefix each with REF-\n\n"

            "STEP 2 — Build the reference string:\n"
            "  • Group numbers of the SAME type together, separated by commas\n"
            "  • Separate different types with ' | '\n"
            "  • Example (3 packing slips + 1 PO): PS-5000087157, 5000087003, 5000087158 | PO-689 SALEM\n"
            "  • Example (2 BOLs): BOL-KP01967, KP01968\n"
            "  • Example (1 BOL + 1 PO): BOL-KP01967 | PO-E260327\n"
            "  • If only 1 number of a type: just show it — PS-5000087157\n\n"

            "STEP 3 — Exclusions:\n"
            "  • Ignore phone numbers, postal codes, item part numbers (SENS0043, HERS0418, etc.)\n"
            "  • Ignore customer account numbers (CUSTOMER NO. field)\n"
            "  • Ignore quantities, weights, prices\n\n"

            "Return ONLY this JSON:\n"
            '{"reference": "PS-5000087157, 5000087003 | PO-689 SALEM"}\n'
            "If truly nothing found: {\"reference\": null}"
        )}] + content_parts

        payload = {
            "model": self._get_model(),
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": 200,
            "temperature": 0,
        }

        raw = _claude_chat(
            messages=payload["messages"],
            max_tokens=payload["max_tokens"],
            api_key=api_key,
            model=payload["model"],
            timeout=60,
        )

        _logger.info("Reference extraction for %s: %s", invoice.name, raw[:300])
        result = self._extract_json_from_text(raw)
        ref = result.get("reference")
        if ref in ("null", "none", "NULL", "NONE", None, ""):
            return None
        return str(ref).strip()

    def analyze_and_generate(self, invoice):
        """
        Analyze all attachments on the invoice and return:
          {
            "service_type": "ltl|ftl|local|other",
            "product_id": <int|null>,
            "reference": "<string>",
            "description": "<string with \\n>",
            "confidence": "high|medium|low"
          }
        """
        api_key = self._get_openai_key()
        if not api_key:
            raise ValueError("OpenAI API key not configured in Settings.")

        content_parts = self._build_content_parts(invoice)
        if not content_parts:
            raise ValueError("No readable attachments found on this invoice.")
        attachment_text = self._collect_attachment_text(invoice)
        weekly_schedule = self._extract_weekly_schedule_details(attachment_text)

        ai_products_text = self._get_ai_products_text()

        # Pull relevant past examples to guide the model
        input_context = self._build_input_context(invoice)
        ml_examples = self._get_ml_examples(input_context)
        ml_section = f"\n\n{ml_examples}" if ml_examples else ""

        system_prompt = (
            "You are a freight logistics invoice assistant for PremaFirm Inc., a Canadian trucking company.\n"
            "Analyze delivery documents (BOLs, picking slips, delivery invoices, route sheets, photos) "
            "and return structured invoice data.\n\n"

            "══ STEP 1 — EXTRACT REFERENCE NUMBERS ══\n"
            "Scan ALL pages for document/reference numbers from these fields:\n"
            "  • Packing Slip NUMBER / Slip # → prefix PS-\n"
            "  • BOL # / Bill of Lading # → prefix BOL-\n"
            "  • Delivery # / Delivery Note # → prefix DEL-\n"
            "  • P.O. NUMBER / Purchase Order # → prefix PO-\n"
            "  • Reference # / Booking # → prefix REF-\n\n"
            "Build the reference string:\n"
            "  • Group same-type numbers together with commas, different types with ' | '\n"
            "  • Examples:\n"
            "    3 packing slips + 1 PO → 'PS-5000087157, 5000087003, 5000087158 | PO-689 SALEM'\n"
            "    BOL + PO → 'BOL-KP01967 | PO-E260327'\n"
            "    Multiple REFs → 'REF-1119363, 1119362, 1119364'\n"
            "  • Ignore: phone numbers, postal codes, part numbers (e.g. SENS0043), customer account numbers\n"
            "  • Nothing found → use JSON null (not the string 'null')\n\n"

            "══ STEP 2 — BUILD DESCRIPTION ══\n"
            "For normal delivery documents, the description should follow this format "
            "(no product/service name — that is already on the invoice line above):\n\n"
            "  Freight / Delivery Service\n"
            "  Route: [origin city, province] → [destination city, province]\n"
            "  Date: [service date, e.g. April 14, 2026]\n\n"
            "Rules:\n"
            "- First line is always 'Freight / Delivery Service'\n"
            "- For multi-stop LTL: Route lists all stops in order "
            "(e.g. 'Newmarket, ON → Aurora, ON → Richmond Hill, ON → Vaughan, ON')\n"
            "- Date is the delivery/service date found in the documents\n"
            "- If the document is a weekly rate confirmation or schedule and does NOT list "
            "explicit destination cities, do NOT invent a route. Use this factual format instead:\n"
            "  Freight / Delivery Service\n"
            "  Schedule: [week range]\n"
            "  Origin: [pickup city, province]\n"
            "  Service: [brief factual summary]\n"
            "- Do NOT include product names, service names, invoice totals, or quantities\n"
            "- Do NOT repeat the reference numbers inside the description\n\n"

            "══ STEP 3 — SELECT SERVICE PRODUCT ══\n"
            "AVAILABLE PRODUCTS (choose product_id from this list, or null if none match):\n"
            + ai_products_text
            + "\n\n══ STEP 4 — EXTRACT AMOUNT ══\n"
            "If the document explicitly includes a grand total, weekly total, or final charge, "
            "return it as a number in `amount` with no currency symbol. "
            "If only daily rates are shown and no grand total is shown, return null.\n"
            + ml_section
        )

        user_content = [
            {
                "type": "text",
                "text": (
                    f"Invoice: {invoice.name} | Partner: {invoice.partner_id.name}\n\n"
                    "Analyze ALL attached documents below.\n"
                    "Follow the 3-step instructions exactly and return ONLY valid JSON:\n"
                    "{\n"
                    '  "service_type": "ltl|ftl|local|other",\n'
                    '  "product_id": <integer id or null>,\n'
                    '  "reference": "<coded reference — e.g. BOL-KP01967 | PO-12345>",\n'
                    '  "description": "Freight / Delivery Service\\nRoute: X -> Y\\nDate: Month DD, YYYY",\n'
                    '  "amount": <number or null>,\n'
                    '  "confidence": "high|medium|low"\n'
                    "}\n\n"
                    "IMPORTANT: description must start with 'Freight / Delivery Service' "
                    "and must NOT contain any product or service name from the invoice. "
                    "If route destinations are not explicitly present, use the schedule-based format instead of guessing."
                ),
            }
        ] + content_parts

        payload = {
            "model": self._get_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 600,
            "temperature": 0.1,
        }

        raw_content = _claude_chat(
            messages=payload["messages"],
            max_tokens=payload["max_tokens"],
            api_key=api_key,
            model=payload["model"],
            timeout=120,
        )

        _logger.info("Invoice AI raw response for %s: %s", invoice.name, raw_content[:500])
        result = self._extract_json_from_text(raw_content)
        # GPT sometimes returns the string "null" instead of JSON null — normalize it
        for key in ("reference", "product_id", "description", "service_type", "confidence", "amount"):
            if result.get(key) in ("null", "none", "NULL", "NONE"):
                result[key] = None
        if weekly_schedule:
            # Weekly schedule PDFs are text-readable but often omit destination stops.
            # Prefer factual PDF-derived values over a guessed route from the model.
            result["description"] = weekly_schedule.get("description") or result.get("description")
            if weekly_schedule.get("reference"):
                result["reference"] = weekly_schedule["reference"]
            if weekly_schedule.get("amount") is not None:
                result["amount"] = weekly_schedule["amount"]
            if weekly_schedule.get("line_items"):
                result["line_items"] = weekly_schedule["line_items"]
        return result
