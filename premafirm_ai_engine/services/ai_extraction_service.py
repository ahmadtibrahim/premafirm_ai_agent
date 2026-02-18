import base64
import io
import json
import logging
import re
from datetime import datetime

import requests

_logger = logging.getLogger(__name__)


class AIExtractionService:
    """Extraction layer that converts broker data into structured freight data."""

    OPENAI_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, env):
        self.env = env

    def _get_openai_key(self):
        return self.env["ir.config_parameter"].sudo().get_param("openai.api_key")

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
            _logger.exception("AI JSON extraction failed")
            return {}

    def _contains_reefer_terms(self, text):
        return bool(re.search(r"reefer|refrigerated|frozen|temperature|temp\s*controlled", text or "", re.I))

    def _load_sections(self, text):
        sections = re.split(r"(?=\bLOAD\s*#)", text or "", flags=re.I)
        return [s.strip() for s in sections if re.search(r"\bLOAD\s*#", s, re.I)]

    def _extract_value(self, text, patterns):
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return (match.group(1) or "").strip()
        return None

    def _extract_labeled_value(self, text, labels):
        escaped = "|".join(labels)
        patterns = [
            rf"(?:{escaped})\s*[:\-]?\s*([^\n]+)",
            rf"(?:{escaped})\s*\n\s*([^\n]+)",
        ]
        return self._extract_value(text, patterns)

    def _coerce_number(self, value):
        if value is None:
            return None
        cleaned = re.sub(r"[^0-9.]", "", str(value))
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:
            return None

    def _parse_load_sections(self, raw_text):
        warnings = []
        errors = []
        stops = []
        pickup_labels = [
            r"Pickup\s*Address",
            r"Pickup\s*Location",
            r"Pickup",
            r"Origin",
            r"Ship\s*From",
            r"Shipper",
        ]
        delivery_labels = [
            r"Delivery\s*Address",
            r"Delivery\s*Location",
            r"Delivery",
            r"Destination",
            r"Ship\s*To",
            r"Receiver",
            r"Consignee",
            r"Drop",
        ]
        delivery_date_labels = [r"Delivery\s*Date", r"Delivery", r"Due\s*Date"]
        pallet_labels = [r"#\s*of\s*Pallets", r"Pallets"]
        weight_labels = [r"Total\s*Weight", r"Weight"]

        for idx, section in enumerate(self._load_sections(raw_text), 1):
            pallets_raw = self._extract_labeled_value(section, pallet_labels)
            size_raw = self._extract_labeled_value(section, [r"Pallet\s*Size"])
            weight_raw = self._extract_labeled_value(section, weight_labels)
            pickup = self._extract_labeled_value(section, pickup_labels)
            delivery = self._extract_labeled_value(section, delivery_labels)
            delivery_date = self._extract_labeled_value(section, delivery_date_labels)

            pallets_val = self._coerce_number(pallets_raw)
            weight_val = self._coerce_number(weight_raw)
            if pallets_raw and pallets_val is None:
                errors.append(f"LOAD #{idx}: invalid pallet count '{pallets_raw}'.")
            if weight_raw and weight_val is None:
                errors.append(f"LOAD #{idx}: invalid weight '{weight_raw}'.")

            if not pickup or not delivery:
                errors.append(f"LOAD #{idx}: missing pickup or delivery location.")
                continue

            sched = None
            if delivery_date:
                try:
                    sched = datetime.fromisoformat(delivery_date.replace("/", "-")).isoformat()
                except Exception:
                    sched = None
                    warnings.append(f"LOAD #{idx}: could not parse delivery date '{delivery_date}'.")

            notes = []
            if size_raw:
                notes.append(f"Pallet size: {size_raw}")

            stops.append(
                {
                    "sequence": (idx * 2) - 1,
                    "stop_type": "pickup",
                    "address": pickup,
                    "pallets": int(pallets_val or 0),
                    "weight_lbs": float(weight_val or 0.0),
                    "scheduled_datetime": None,
                    "special_instructions": "; ".join(notes) if notes else None,
                }
            )
            stops.append(
                {
                    "sequence": idx * 2,
                    "stop_type": "delivery",
                    "address": delivery,
                    "pallets": int(pallets_val or 0),
                    "weight_lbs": float(weight_val or 0.0),
                    "scheduled_datetime": sched,
                    "special_instructions": "; ".join(notes) if notes else None,
                }
            )

        return {"stops": stops, "warnings": warnings, "errors": errors}

    def _extract_attachment_text(self, attachment):
        name = (attachment.name or "").lower()
        payload = base64.b64decode(attachment.datas or b"") if attachment.datas else b""
        if not payload:
            return ""

        if name.endswith(".pdf"):
            try:
                import pypdf

                reader = pypdf.PdfReader(io.BytesIO(payload))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception:
                _logger.exception("PDF parsing failed for attachment %s", attachment.name)
                return ""

        if name.endswith(".xlsx") or name.endswith(".xls"):
            try:
                import openpyxl

                workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
                rows = []
                for sheet in workbook.worksheets:
                    for row in sheet.iter_rows(values_only=True):
                        vals = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                        if vals:
                            rows.append(" | ".join(vals))
                return "\n".join(rows)
            except Exception:
                _logger.exception("Excel parsing failed for attachment %s", attachment.name)
                return ""

        return ""

    def _fallback_parse(self, email_text):
        pickup = None
        delivery = None
        for line in (email_text or "").splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            low = cleaned.lower()
            if not pickup and any(k in low for k in ("pickup", "pu", "shipper")):
                pickup = cleaned.split(":", 1)[-1].strip()
            elif not delivery and any(k in low for k in ("delivery", "drop", "receiver", "consignee")):
                delivery = cleaned.split(":", 1)[-1].strip()
        stops = []
        if pickup:
            stops.append({"stop_type": "pickup", "address": pickup})
        if delivery:
            stops.append({"stop_type": "delivery", "address": delivery})
        return {
            "stops": stops,
            "inside_delivery": "inside delivery" in (email_text or "").lower(),
            "liftgate": "liftgate" in (email_text or "").lower(),
            "detention_requested": "detention" in (email_text or "").lower(),
            "reefer_required": self._contains_reefer_terms(email_text),
            "warnings": ["AI extraction unavailable, used fallback parser."],
        }

    def _openai_extract(self, source_text, source_label):
        api_key = self._get_openai_key()
        if not api_key:
            return {}

        system_prompt = (
            "You are a senior freight-dispatch AI. Parse broker load details and return ONLY JSON with stops, "
            "weights, pallets, and accessorials."
        )
        user_prompt = f"""
Extract freight details from this {source_label} and return valid JSON:
{{
  "stops": [{{"stop_type":"pickup|delivery","address":"string","pallets":number|null,"weight_lbs":number|null}}],
  "inside_delivery": boolean,
  "liftgate": boolean,
  "detention_requested": boolean,
  "cross_border": boolean,
  "reefer_required": boolean,
  "total_weight_lbs": number|null,
  "total_pallets": number|null,
  "warnings": ["text"]
}}

CONTENT:
{source_text}
"""
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": 0.1,
        }
        try:
            response = requests.post(
                self.OPENAI_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return self._extract_json_from_text(content)
        except Exception:
            _logger.exception("OpenAI extraction failed")
            return {}

    def extract_load(self, email_text, attachments=None):
        attachments = attachments or self.env["ir.attachment"]
        parsable = attachments.filtered(lambda a: (a.name or "").lower().endswith((".pdf", ".xlsx", ".xls")))
        if parsable:
            raw_text = "\n".join(filter(None, [self._extract_attachment_text(att) for att in parsable]))
            parsed = self._parse_load_sections(raw_text)
            if not parsed.get("stops") and raw_text.strip():
                structured = self._openai_extract(raw_text, "attachment text")
                if structured.get("stops"):
                    structured.setdefault("warnings", [])
                    structured["warnings"].append("Attachment data used as primary source; email body ignored.")
                    structured.update(
                        {
                            "inside_delivery": bool(structured.get("inside_delivery")),
                            "liftgate": bool(structured.get("liftgate")),
                            "detention_requested": bool(structured.get("detention_requested")),
                            "reefer_required": bool(structured.get("reefer_required") or self._contains_reefer_terms(raw_text)),
                            "source": "attachment",
                            "notes": "Attachment data used as primary source; email body ignored.",
                        }
                    )
                    return structured

            if parsed.get("stops"):
                parsed.update(
                    {
                        "inside_delivery": "inside delivery" in raw_text.lower(),
                        "liftgate": "liftgate" in raw_text.lower(),
                        "detention_requested": "detention" in raw_text.lower(),
                        "reefer_required": self._contains_reefer_terms(raw_text),
                        "source": "attachment",
                        "notes": "Attachment data used as primary source; email body ignored.",
                    }
                )
                parsed["warnings"] = list(parsed.get("warnings") or [])
                parsed["warnings"].append("Attachment data used as primary source; email body ignored.")
                return parsed

        structured_email = self._openai_extract(email_text, "email")
        if structured_email.get("stops"):
            structured_email["source"] = "email"
            return structured_email

        parsed = self._fallback_parse(email_text)
        parsed["source"] = "email"
        if parsable:
            parsed.setdefault("warnings", []).append("Attachment parsing and AI attachment extraction failed; used email fallback.")
        return parsed
