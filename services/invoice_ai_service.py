import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile

import requests
from .deepseek_utils import (
    deepseek_chat,
    get_api_key,
    get_model,
    today_context_line as _today_context_line,
)

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

    def _get_api_key(self):
        """Read DeepSeek API key from system parameters."""
        return get_api_key(self.env)

    def _get_model(self):
        """Read preferred DeepSeek model from system parameters."""
        return get_model(self.env)

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

    def _pdf_ocr_text(self, pdf_bytes, max_pages=4):
        """Extract text from a scanned/image-based PDF using OCR.

        Returns combined text from up to *max_pages* pages, or empty string
        on failure.  Uses pdf2image → pytesseract.
        """
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
            images = convert_from_bytes(pdf_bytes, dpi=200, fmt="jpeg")
            texts = []
            for img in images[:max_pages]:
                page_text = pytesseract.image_to_string(img, lang="eng")
                if page_text and page_text.strip():
                    texts.append(page_text.strip())
            return "\n\n".join(texts)
        except Exception:
            _logger.exception("PDF OCR failed")
            return ""

    def _image_ocr_text(self, image_bytes):
        """OCR a single image file, returning extracted text or empty string."""
        try:
            from PIL import Image
            import pytesseract
            img = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(img, lang="eng")
            return (text or "").strip()
        except Exception:
            _logger.exception("Image OCR failed")
            return ""

    def _extract_rate_conf_schedule_table(self, pdf_bytes):
        """
        Extract weekly schedule line items from a rate confirmation PDF using
        pdfplumber's table extraction.  Handles multi-line date cells (e.g.
        "Monday, May 04,\\n2026") and split activity rows (e.g. Friday's
        "Delivery Route — Final" on one row and "Day" on the next).

        Returns list of {"name": "DayName, Mon DD, YYYY - Activity", "amount": float}
        or empty list if extraction fails.
        """
        try:
            import pdfplumber
        except ImportError:
            return []

        day_re = re.compile(
            r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]", re.I
        )
        year_re = re.compile(r"^(20\d{2})$")
        amount_re = re.compile(r"\$([\d,]+\.\d{2})")

        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        if not table:
                            continue
                        # Only process the weekly-schedule table
                        rows_flat = [
                            " ".join(str(c or "") for c in row)
                            for row in table
                        ]
                        if not any(
                            "Delivery Route" in r or "Pickup only" in r
                            for r in rows_flat
                        ):
                            continue

                        rows_out = []
                        i = 0
                        while i < len(table):
                            row = table[i]
                            cells = [
                                re.sub(r"\s+", " ", str(c or "")).strip()
                                for c in row
                            ]

                            # Stop at TOTAL WEEK
                            if any("TOTAL" in c.upper() for c in cells if c):
                                break

                            # Find a cell that starts with a day name
                            date_part = ""
                            for c in cells:
                                if day_re.match(c):
                                    date_part = c
                                    break

                            if not date_part:
                                i += 1
                                continue

                            # Scan up to 3 rows ahead to accumulate year, activity, amount
                            date_text = date_part
                            activity = ""
                            amount = None

                            for j in range(i, min(i + 3, len(table))):
                                scan_cells = [
                                    re.sub(r"\s+", " ", str(c or "")).strip()
                                    for c in table[j]
                                ]
                                scan_flat = " ".join(sc for sc in scan_cells if sc)
                                scan_lower = scan_flat.replace("—", "-").lower()

                                # Append year if missing from date
                                if not re.search(r"\d{4}", date_text):
                                    for c in scan_cells:
                                        if year_re.match(c):
                                            date_text = date_text.rstrip(",") + f", {c}"
                                            break

                                # Identify activity
                                if not activity:
                                    if "pickup only" in scan_lower:
                                        activity = "Pickup only - No delivery"
                                    elif "final" in scan_lower and "delivery route" in scan_lower:
                                        activity = "Delivery Route - Final Day"
                                    elif "final" in scan_lower:
                                        # "Final" may appear alone when the row is split
                                        activity = "Delivery Route - Final Day"
                                    elif "delivery route" in scan_lower:
                                        activity = "Delivery Route + Pickup"

                                # Find dollar amount (sane daily-rate range)
                                if amount is None:
                                    for a in amount_re.findall(scan_flat):
                                        try:
                                            val = float(a.replace(",", ""))
                                            if 10 < val < 9_000:
                                                amount = val
                                                break
                                        except ValueError:
                                            pass

                            if activity and amount is not None:
                                name = f"{date_text} - {activity}"
                                rows_out.append({"name": name, "amount": amount})

                            i += 1

                        if rows_out:
                            return rows_out
        except Exception:
            _logger.exception("Rate confirmation table extraction failed")
        return []

    def _find_rate_conf_bytes(self, invoice):
        """Return bytes of the rate-confirmation PDF on this invoice, or None."""
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        for att in attachments:
            if not (att.name or "").lower().endswith(".pdf"):
                continue
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes:
                continue
            text = self._pdf_extract_text(file_bytes)
            if text and "rate confirmation" in text.lower() and "total week" in text.lower():
                return file_bytes
        return None

    def _extract_pickup_origin(self, text):
        """Extract the pickup city/province from combined attachment text."""
        m = re.search(
            r"Pickup Address[^\n]+,\s*([A-Za-z][A-Za-z ]+),\s*Ontario",
            text, re.I,
        )
        if m:
            return f"{m.group(1).strip()}, ON"
        return ""

    def _extract_trip_sheet_routes(self, invoice):
        """
        Scan attachments for Driver Trip Sheets and return a mapping of
        weekday name → ordered unique city string extracted from delivery addresses.

        Example: {"Tuesday": "Burlington, ON → Milton, ON → Acton, ON"}
        """
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ])
        city_re = re.compile(r",\s+([A-Z][A-Z ]+?)\s+ON\b", re.I)
        routes = {}

        for att in sorted(attachments, key=lambda a: a.id):
            if not (att.name or "").lower().endswith(".pdf"):
                continue
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes:
                continue
            text = self._pdf_extract_text(file_bytes)
            if not text or "Driver Trip Sheet" not in text:
                continue
            # Extract service date
            date_m = re.search(r"Start Date\s+(\d{4}-\d{2}-\d{2})", text)
            if not date_m:
                continue
            try:
                from datetime import date as _date
                d = _date.fromisoformat(date_m.group(1))
                weekday = d.strftime("%A")
            except Exception:
                continue
            # Extract unique cities in order of first appearance
            seen: dict = {}
            for m in city_re.finditer(text):
                city = m.group(1).strip().title()
                if len(city) > 2 and city not in seen:
                    seen[city] = True
            if seen:
                routes[weekday] = " → ".join(f"{c}, ON" for c in seen)

        return routes

    # ── Attachment type helpers ─────────────────────────────────────────────

    _STOPS_LIST_KEYWORDS = (
        "scan", "stop", "route", "manifest", "list", "tender", "bol",
        "waybill", "ratecon", "rate_con", "confirmation", "schedule",
        "load", "run", "delivery_list", "pickup",
    )
    _POD_KEYWORDS = ("pod", "proof_of", "proof of", "signed", "del_photo", "delivery_photo")

    def _att_priority(self, att_name):
        """0 = stops-list/route sheet (highest), 1 = generic doc, 2 = POD photo (lowest)."""
        n = (att_name or "").lower()
        if any(kw in n for kw in self._STOPS_LIST_KEYWORDS):
            return 0
        if any(kw in n for kw in self._POD_KEYWORDS):
            return 2
        return 1

    def _att_label(self, att_name):
        """Return a descriptive label so the AI knows the purpose of each attachment."""
        pri = self._att_priority(att_name)
        if pri == 0:
            return (
                f"[STOPS LIST / ROUTE DOCUMENT: {att_name}]"
                " — PRIMARY source for all reference numbers (PS#, BOL#, PO#) and delivery stops."
                " Use this attachment to determine the route and stops."
            )
        if pri == 2:
            return (
                f"[POD PHOTO: {att_name}]"
                " — Proof-of-delivery photo. Use ONLY to confirm date or signature."
                " Do NOT extract route stops or reference numbers from this image."
            )
        return f"[DOCUMENT: {att_name}]"

    def _build_content_parts(self, invoice):
        """Build the GPT-4o content array from all invoice attachments.

        Stops-list / route-sheet files are sorted first and labeled so the AI
        always uses them as the primary source for stop/route information instead
        of reading addresses from POD photos.
        """
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", invoice.id),
        ], order="id asc")

        # Sort: stops-list first (priority 0), then generic docs (1), then POD photos last (2)
        sorted_atts = sorted(attachments, key=lambda a: self._att_priority(a.name))

        content_parts = []
        image_mimes = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        image_count = 0  # track images separately so labels don't count toward the cap

        for att in sorted_atts:
            # Image-only attachments are capped; text PDFs never count against the cap
            # so we use continue (not break) to keep processing remaining text files.
            if image_count >= MAX_IMAGE_PARTS:
                continue
            file_bytes = self._get_attachment_bytes(att)
            if not file_bytes:
                continue

            name = (att.name or "").lower()
            label = self._att_label(att.name)

            # Direct image files
            matched_mime = next(
                (mime for ext, mime in image_mimes.items() if name.endswith(ext)),
                None,
            )
            if matched_mime:
                # DeepSeek chat models are text-only — OCR image instead
                ocr_text = self._image_ocr_text(file_bytes)
                if ocr_text:
                    content_parts.append({
                        "type": "text",
                        "text": f"{label}\n[OCR extracted text from image]\n{ocr_text}",
                    })
                else:
                    content_parts.append({
                        "type": "text",
                        "text": f"{label}\n[Unable to read this image — OCR failed]",
                    })
                image_count += 1
                continue

            # PDF: try text extraction first, fall back to OCR
            if name.endswith(".pdf"):
                text = self._pdf_extract_text(file_bytes)
                if text and len(text) > 80:
                    content_parts.append({"type": "text", "text": f"{label}\n{text}"})
                else:
                    # Scanned PDF — use OCR to extract text (DeepSeek
                    # chat models are text-only, no image_url support)
                    ocr_text = self._pdf_ocr_text(file_bytes)
                    if ocr_text and len(ocr_text) > 80:
                        content_parts.append({
                            "type": "text",
                            "text": f"{label}\n[OCR extracted text follows]\n{ocr_text}",
                        })
                    else:
                        content_parts.append({
                            "type": "text",
                            "text": f"{label}\n[Unable to read this PDF — no text layer and OCR failed]",
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

    def _detect_tax_mentioned(self, text):
        """Return True if the document explicitly mentions any applicable tax."""
        if not text:
            return False
        lower = text.lower()
        return bool(re.search(r'\b(hst|gst|pst|qst|vat|tax|taxes)\b', lower))

    def _detect_uom(self, text, service_type=""):
        """
        Return 'loads' or 'pallets' based on document content.

        Per-pallet pricing (rate quoted per pallet) → 'pallets'.
        Route/trip-based flat rates, rate confirmations, delivery routes → 'loads'.
        Defaults to 'loads' for all freight services.
        """
        if not text:
            return "loads"
        lower = text.lower()
        if re.search(r'\$[\d,.]+\s*(?:per|/)\s*pallet', lower):
            return "pallets"
        if any(kw in lower for kw in (
            "delivery route", "pickup only", "rate confirmation",
            "per load", "per trip", "per run",
        )):
            return "loads"
        if service_type in ("ftl", "local"):
            return "loads"
        return "loads"

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

    def _build_past_invoices_context(self, invoice):
        """Return a formatted summary of the last 5 posted invoices for this customer."""
        if not invoice.partner_id:
            return ""
        domain = [
            ("partner_id", "=", invoice.partner_id.id),
            ("move_type", "=", invoice.move_type),
            ("state", "in", ["posted", "cancel"]),
        ]
        if invoice.id:
            domain.append(("id", "!=", invoice.id))
        past = self.env["account.move"].search(domain, order="invoice_date desc, id desc", limit=5)
        if not past:
            return ""
        lines = []
        for inv in past:
            lines.append(
                f"Invoice: {inv.name} | Date: {inv.invoice_date or inv.date or 'N/A'} | Ref: {inv.ref or '—'}"
            )
            for line in inv.invoice_line_ids.filtered(lambda l: l.display_type == "product"):
                lines.append(
                    f"  Product: {line.product_id.name or '?'} | ${line.price_unit:.2f} × {line.quantity:.0f}"
                )
            for line in inv.invoice_line_ids.filtered(lambda l: l.display_type == "line_note"):
                note = (line.name or "").strip()
                if note:
                    lines.append(f"  Note: {note[:300]}")
            lines.append("")
        return "\n".join(lines)

    def analyze_from_text(self, invoice, text, past_context=""):
        """
        Analyze a pasted WhatsApp/text message and return structured invoice data
        in the same format as analyze_and_generate().
        """
        api_key = self._get_api_key()
        if not api_key:
            raise ValueError("DeepSeek API key not configured in Settings.")
        if not text or not text.strip():
            raise ValueError("No text provided to analyze.")

        ai_products_text = self._get_ai_products_text()
        input_context = f"Partner: {invoice.partner_id.name or ''} | Text: {text[:200]}"
        ml_examples = self._get_ml_examples(input_context)
        ml_section = f"\n\n{ml_examples}" if ml_examples else ""
        past_section = (
            f"\n\n══ LAST 5 INVOICES FOR THIS CUSTOMER — match their format and style ══\n{past_context}"
            if past_context else ""
        )

        system_prompt = (
            "You are a freight logistics invoice assistant for PremaFirm Inc., a Canadian trucking company.\n"
            "A customer sent a WhatsApp or text message describing a delivery job. "
            "Extract reference numbers and generate a professional invoice description.\n\n"
            f"{_today_context_line()}\n\n"

            "══ STEP 1 — REFERENCE (ALWAYS GENERATE ONE) ══\n"
            "First check if the message already contains a reference number or code "
            "(PO numbers, order numbers, job/load/BOL numbers, delivery numbers, etc.).\n"
            "  • If found: prefix it (PO-, BOL-, REF-) and use it as-is.\n"
            "  • If NOT found: INVENT a short internal reference from the available context:\n"
            "      Format: [service_prefix]-[stop_codes]-[date_DDMMYY]\n"
            "      service_prefix: R = Reefer, D = Dry Van, F = Flatbed, L = LTL, use D if unknown\n"
            "      stop_codes: first 3 letters of each city, up to 4 stops (e.g. AJX-OSH-NCL-LND)\n"
            "      date: service date as DDMMYY, or today's date if no date mentioned\n"
            "      Example invented refs: D-AJX-OSH-220526, R-MIS-TOR-BRH-210526, L-CAL-EDM-200526\n"
            "  • NEVER return null — always produce a reference string.\n\n"

            "══ STEP 2 — BUILD DESCRIPTION ══\n"
            "Generate a professional description starting with 'Freight / Delivery Service'.\n"
            "Format:\n"
            "  Freight / Delivery Service\n"
            "  Route: [origin city, province] → [destination city, province]\n"
            "  Date: [service date]\n"
            "  [optional: pallets, weight, special instructions, or service notes]\n"
            "Rules:\n"
            "  • First line is always 'Freight / Delivery Service'\n"
            "  • Only include route info that is explicitly mentioned — do NOT guess\n"
            "  • Use the past invoice examples below to match formatting style for this customer\n\n"

            "══ STEP 3 — SELECT SERVICE PRODUCT ══\n"
            "AVAILABLE PRODUCTS:\n"
            + ai_products_text
            + "\n\n══ STEP 4 — EXTRACT AMOUNT ══\n"
            "If the message states a rate or total, return as a number. Otherwise null.\n"
            + past_section
            + ml_section
        )

        user_message = (
            f"Customer: {invoice.partner_id.name}\n"
            f"Reference: {invoice.name or 'Draft'}\n\n"
            f"Customer's message:\n{text.strip()}\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "service_type": "ltl|ftl|local|other",\n'
            '  "product_id": <integer id or null>,\n'
            '  "reference": "<coded reference — always a string, never null>",\n'
            '  "description": "Freight / Delivery Service\\nRoute: X → Y\\nDate: Month DD, YYYY",\n'
            '  "amount": <number or null>,\n'
            '  "confidence": "high|medium|low",\n'
            '  "scheduled_date": "YYYY-MM-DD or null",\n'
            '  "commodity": "<what is being shipped, e.g. Groceries, Auto Parts, or null>",\n'
            '  "requires_reefer": <true|false>,\n'
            '  "requires_liftgate": <true|false>,\n'
            '  "temp_requirement": "<temperature spec if reefer, e.g. -18 or 2 to 8, else null>",\n'
            '  "approximate_skids": <total pallets across all pickups, integer or 0>,\n'
            '  "max_onboard_pallets": <peak pallet count on truck at one time, integer or 0>,\n'
            '  "stops": [\n'
            '    {\n'
            '      "type": "pickup|dropoff",\n'
            '      "address": "full civic address including city and province",\n'
            '      "pallets_in": <integer — pallets LOADED at this pickup stop; 0 for dropoffs>,\n'
            '      "pallets_out": <integer — pallets UNLOADED at this dropoff stop; 0 for pickups>,\n'
            '      "liftgate": <true|false — does this stop need a liftgate?>,\n'
            '      "notes": "<customer notes or special instructions, empty string if none>",\n'
            '      "scheduled_time": "YYYY-MM-DDTHH:MM or null",\n'
            '      "linked_load_group": <integer round — 1 for first pickup+dropoffs, 2 for second pickup+dropoffs>\n'
            '    }\n'
            '  ]\n'
            "}\n"
            "STOPS RULES:\n"
            "- Extract only stops that are explicitly mentioned. Return empty array if fewer than 2.\n"
            "- STOP TYPES:\n"
            "  - type='pickup': driver loads freight. Set pallets_in = pallets loaded, pallets_out = 0.\n"
            "  - type='dropoff': driver delivers freight. Set pallets_out = pallets delivered, pallets_in = 0.\n"
            "  - IMPORTANT: If the text says 'return to [same address]' or 'back to [warehouse]' for another load,\n"
            "    that is ALSO type='pickup' (a second round pickup), NOT a dropoff.\n"
            "- TIME WINDOW TYPES — set time_window_type for EACH stop:\n"
            "  - 'exact': a specific fixed appointment time (e.g. '9:00 AM', 'must arrive at 2:00 PM')\n"
            "  - 'deadline': must be completed BY a time (e.g. 'by 4 PM', 'no later than', 'before', 'max')\n"
            "    → also set deadline_time = that datetime, hard_deadline = true\n"
            "  - 'window': a time range (e.g. '7:00 AM – 8:00 AM', 'between 9 and 11')\n"
            "    → set earliest_time = window open, latest_time = window close\n"
            "  - 'flexible': no time constraint mentioned\n"
            "- DOCK/DOOR: if a dock door, bay, or unit number is mentioned separately from the civic address\n"
            "  (e.g. 'Door 7', 'Bay 3', 'Unit 12', 'Door 7-11'), extract it as dock_door.\n"
            "- MULTI-DAY: if pickup and delivery are on DIFFERENT calendar dates, preserve the correct\n"
            "  date in scheduled_time for each stop (do NOT force all stops to the same date).\n"
            "- MULTI-ROUND: If truck returns to the same warehouse for a second pickup, that is round 2.\n"
            "  linked_load_group=1 → first pickup and all its dropoffs.\n"
            "  linked_load_group=2 → second pickup and all its dropoffs. And so on.\n"
            "- max_onboard_pallets: compute the running pallet total (add pallets_in, subtract pallets_out at each stop). Return the peak value.\n"
            "- approximate_skids: sum of all pallets_in (total pallets picked up across all rounds).\n"
            "- Set requires_reefer=true if temperature-controlled or frozen/chilled is mentioned.\n"
            "- Set requires_liftgate=true if any stop mentions 'no dock', 'tailgate', or 'liftgate'.\n"
            "- SERVICE TYPE: set service_type based on context:\n"
            "  - 'dedicated': single customer, truck blocked for a full or half day, flat rate\n"
            "  - 'ftl': full truckload, single customer destination\n"
            "  - 'ltl': multiple customers sharing truck, or small partial loads\n"
            "  - 'local': short-haul same-day, single city\n"
            "  - 'other': if unclear\n"
            "\nFor each stop, the full schema is:\n"
            '  {\n'
            '    "type": "pickup|dropoff",\n'
            '    "address": "full civic address including city and province",\n'
            '    "dock_door": "<dock or door number, empty string if none>",\n'
            '    "pallets_in": <integer>,\n'
            '    "pallets_out": <integer>,\n'
            '    "liftgate": <true|false>,\n'
            '    "notes": "<special instructions>",\n'
            '    "scheduled_time": "YYYY-MM-DDTHH:MM or null",\n'
            '    "time_window_type": "flexible|exact|deadline|window",\n'
            '    "deadline_time": "YYYY-MM-DDTHH:MM or null",\n'
            '    "earliest_time": "YYYY-MM-DDTHH:MM or null",\n'
            '    "latest_time": "YYYY-MM-DDTHH:MM or null",\n'
            '    "hard_deadline": <true|false>,\n'
            '    "linked_load_group": <integer round number, 0 if not multi-round>\n'
            '  }'
        )

        raw_content = deepseek_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1400,
            api_key=api_key,
            model=self._get_model(),
            timeout=60,
        )

        _logger.info("WhatsApp text AI response for %s: %s", invoice.name, raw_content[:500])
        result = self._extract_json_from_text(raw_content)
        for key in ("reference", "product_id", "description", "service_type", "confidence", "amount",
                    "commodity", "temp_requirement"):
            if result.get(key) in ("null", "none", "NULL", "NONE"):
                result[key] = None
        for key in ("scheduled_date",):
            if result.get(key) in ("null", "none", "NULL", "NONE", ""):
                result[key] = None
        if not isinstance(result.get("stops"), list):
            result["stops"] = []
        # Ensure each stop has the new fields (backwards compat)
        for stop in result["stops"]:
            if "pallets_in" not in stop and "pallets_out" not in stop:
                legacy = int(stop.get("pallets") or 0)
                stop["pallets_in"] = legacy if stop.get("type") == "pickup" else 0
                stop["pallets_out"] = legacy if stop.get("type") != "pickup" else 0
            stop.setdefault("pallets_in", 0)
            stop.setdefault("pallets_out", 0)
            stop.setdefault("linked_load_group", 0)
        result["tax_mentioned"] = self._detect_tax_mentioned(text)
        result["uom"] = self._detect_uom(text, result.get("service_type") or "")
        return result

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
        api_key = self._get_api_key()
        if not api_key:
            raise ValueError("DeepSeek API key not configured in Settings.")

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

        raw = deepseek_chat(
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
        api_key = self._get_api_key()
        if not api_key:
            raise ValueError("DeepSeek API key not configured in Settings.")

        content_parts = self._build_content_parts(invoice)
        if not content_parts:
            raise ValueError("No readable attachments found on this invoice.")
        attachment_text = self._collect_attachment_text(invoice)
        weekly_schedule = self._extract_weekly_schedule_details(attachment_text)

        # ── Enhanced weekly schedule extraction ────────────────────────────
        # The text-based heuristic misparsing multi-column PDF table layouts
        # (pdfplumber interleaves columns, splitting "2026" to the next line).
        # Use pdfplumber's table extraction which preserves cell boundaries.
        if weekly_schedule:
            rate_conf_bytes = self._find_rate_conf_bytes(invoice)
            if rate_conf_bytes:
                table_items = self._extract_rate_conf_schedule_table(rate_conf_bytes)
                if table_items:
                    weekly_schedule["line_items"] = table_items
                    _logger.info(
                        "Table extraction yielded %d line items for %s",
                        len(table_items), invoice.name,
                    )
            # Correlate trip sheets with rate confirmation days to add route info
            trip_routes = self._extract_trip_sheet_routes(invoice)
            if trip_routes and weekly_schedule.get("line_items"):
                origin = self._extract_pickup_origin(attachment_text)
                for item in weekly_schedule["line_items"]:
                    day_m = re.match(
                        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)",
                        item.get("name", ""), re.I,
                    )
                    if not day_m:
                        continue
                    weekday = day_m.group(1).capitalize()
                    route = trip_routes.get(weekday)
                    if not route:
                        continue
                    # Skip pickup-only days (no delivery route to show)
                    if "pickup only" in item.get("name", "").lower():
                        continue
                    route_prefix = f"{origin} → " if origin else ""
                    item["name"] = item["name"] + f"\nRoute: {route_prefix}{route}"

        ai_products_text = self._get_ai_products_text()

        # Pull relevant past examples to guide the model
        input_context = self._build_input_context(invoice)
        ml_examples = self._get_ml_examples(input_context)
        ml_section = f"\n\n{ml_examples}" if ml_examples else ""

        system_prompt = (
            "You are a freight logistics invoice assistant for PremaFirm Inc., a Canadian trucking company.\n"
            "Analyze delivery documents (BOLs, picking slips, delivery invoices, route sheets, photos) "
            "and return structured invoice data.\n\n"
            f"{_today_context_line()} If a document shows a date with no year (or an ambiguous/relative "
            "date like a bare weekday), resolve it against this actual current date — never assume a "
            "different year.\n\n"

            "══ ATTACHMENT TYPES — READ THIS FIRST ══\n"
            "The documents below are labeled by type:\n"
            "  [STOPS LIST / ROUTE DOCUMENT] — PRIMARY source. "
            "Extract ALL reference numbers (PS#, BOL#, PO#) and route/stop information "
            "ONLY from this document. If one is present, it overrides everything else.\n"
            "  [POD PHOTO] — Proof-of-delivery photo. Use ONLY to confirm the service date "
            "or a customer signature. Do NOT read addresses or stop numbers from POD photos.\n"
            "  [DOCUMENT] — Supporting document, use as needed.\n\n"
            "RULE: If a [STOPS LIST / ROUTE DOCUMENT] is attached, ignore POD photos "
            "for reference numbers and route stops entirely.\n\n"

            "══ STEP 1 — EXTRACT REFERENCE NUMBERS ══\n"
            "From the [STOPS LIST / ROUTE DOCUMENT] (or the only document if there is no stops list), "
            "scan for document/reference numbers:\n"
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
            "  • Nothing found → use JSON null (not the string 'null' or the word 'None')\n\n"

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
            "- Derive the route from the [STOPS LIST / ROUTE DOCUMENT] only — "
            "never from POD photos\n"
            "- If the document is a weekly rate confirmation or schedule and does NOT list "
            "explicit destination cities, do NOT invent a route. Use this factual format instead:\n"
            "  Freight / Delivery Service\n"
            "  Schedule: [week range]\n"
            "  Origin: [pickup city, province]\n"
            "  Service: [brief factual summary]\n"
            "- Do NOT include product names, service names, invoice totals, or quantities\n"
            "- Do NOT repeat the reference numbers inside the description\n"
            "- NEVER start the description or any field with the word 'None'\n\n"

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

        raw_content = deepseek_chat(
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
            if result.get(key) in ("null", "none", "NULL", "NONE", "None"):
                result[key] = None
        # Strip accidental leading "None" prefix — AI occasionally returns "NonePS-12345"
        # when the previous reference was empty/null and it leaks into the new value
        for key in ("reference", "description"):
            val = result.get(key)
            if isinstance(val, str):
                stripped = val.lstrip()
                if stripped.startswith("None") and len(stripped) > 4 and stripped[4:5].isupper():
                    result[key] = stripped[4:].lstrip()
                    _logger.warning(
                        "Stripped 'None' prefix from %s on invoice %s: '%s' → '%s'",
                        key, invoice.name, stripped[:60], result[key][:60],
                    )
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
        # Detect tax and UoM from the attachment content
        result["tax_mentioned"] = self._detect_tax_mentioned(attachment_text)
        result["uom"] = self._detect_uom(attachment_text, result.get("service_type") or "")
        return result
