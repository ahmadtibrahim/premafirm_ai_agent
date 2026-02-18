import json
import logging
import re

import requests

_logger = logging.getLogger(__name__)


class AIExtractionService:
    """Extraction layer that converts messy broker email text into structured freight data."""

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

    def _fallback_parse(self, email_text):
        """Minimal fallback parser when AI is unavailable."""
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
        po_match = re.search(r"(?:PO|Purchase\s*Order)\s*[:#-]?\s*([A-Z0-9-]+)", email_text or "", re.I)
        return {
            "stops": stops,
            "inside_delivery": "inside delivery" in (email_text or "").lower(),
            "liftgate": "liftgate" in (email_text or "").lower(),
            "detention_requested": "detention" in (email_text or "").lower(),
            "service_type": "reefer" if re.search(r"reefer|frozen|temperature|temp controlled", email_text or "", re.I) else "dry",
            "premafirm_po": po_match.group(1) if po_match else None,
            "warnings": ["AI extraction unavailable, used fallback parser."],
        }

    def extract_load(self, email_text):
        api_key = self._get_openai_key()
        if not api_key:
            return self._fallback_parse(email_text)

        system_prompt = (
            "You are a senior freight-dispatch AI. Parse messy broker emails and return ONLY JSON. "
            "Ignore signatures/disclaimers/quoted chains. Detect multi-pick and multi-delivery, "
            "cross-dock, reefer, liftgate, inside delivery, detention, cross-border, and time windows."
        )
        user_prompt = f"""
Extract freight details from the following email.

Return this JSON schema exactly:
{{
  "stops": [
    {{
      "stop_type": "pickup|delivery",
      "address": "full best-available address",
      "pallets": number|null,
      "weight_lbs": number|null,
      "service_type": "dry|reefer|null",
      "window_start": "ISO datetime or null",
      "window_end": "ISO datetime or null",
      "special_instructions": "string or null"
    }}
  ],
  "inside_delivery": boolean,
  "liftgate": boolean,
  "detention_requested": boolean,
  "cross_border": boolean,
  "total_weight_lbs": number|null,
  "total_pallets": number|null,
  "notes": "short dispatch summary",
  "warnings": ["warning text"]
}}

Rules:
- Support multiple pickups and deliveries.
- Infer missing pallets from weight with 1 pallet ~ 1800 lbs when reasonable.
- If service type unclear use dry; reefer if frozen/temperature-controlled words appear.
- Handle ASAP/same day/morning language into window fields where possible.
- Return valid JSON only.

EMAIL:
{email_text}
"""

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }

        try:
            response = requests.post(
                self.OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            structured = self._extract_json_from_text(content)
            if not structured.get("stops"):
                fallback = self._fallback_parse(email_text)
                fallback["warnings"].append("AI response missing stops.")
                return fallback
            return structured
        except Exception:
            _logger.exception("OpenAI extraction failed")
            return self._fallback_parse(email_text)
