import json
import logging
import math
import re

import requests
from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.mapbox_service import MapboxService
from ..services.pricing_engine import PricingEngine


_logger = logging.getLogger(__name__)


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _clean_body(self):
        """Return latest customer email text, preferring body_plaintext over HTML body."""
        messages = self.message_ids.sorted("date", reverse=True)
        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue

            # Prioritize true incoming emails from customers.
            if msg.message_type not in ("email", "comment"):
                continue

            plain = (msg.body_plaintext or "").strip()
            if plain:
                return plain

            if msg.body:
                cleaned_html = (html2plaintext(msg.body) or "").strip()
                if cleaned_html:
                    return cleaned_html
        return ""

    @staticmethod
    def _fallback_extract_stops(email_text):
        """Fallback extraction when model output is empty/invalid.

        Handles plain patterns like:
        - Pickup: city, state zip
        - Delivery: city, state zip
        - LOAD #1 / LOAD #2 blocks
        """
        text = email_text or ""
        if not text.strip():
            return []

        def _clean_addr(raw):
            return re.sub(r"\s+", " ", (raw or "").strip(" -:\n\t"))

        stops = []
        # Block-based parsing for multi-load emails.
        load_blocks = re.split(r"\bLOAD\s*#?\d+\b", text, flags=re.I)
        if len(load_blocks) > 1:
            for block in load_blocks[1:]:
                pickup_match = re.search(r"pickup\s*:\s*(.+?)(?:\n\s*\n|\n\s*delivery\s*:|$)", block, re.I | re.S)
                delivery_match = re.search(r"delivery\s*:\s*(.+?)(?:\n\s*\n|\n\s*delivery\s*date\s*:|$)", block, re.I | re.S)
                pallets_match = re.search(r"pallets?\s*[:#]?\s*([\d,]+)", block, re.I)
                weight_match = re.search(r"weight\s*[:#]?\s*([\d,\.]+)\s*(?:lbs?|lb)?", block, re.I)

                pallets = int((pallets_match.group(1) if pallets_match else "0").replace(",", "") or 0)
                weight_lbs = float((weight_match.group(1) if weight_match else "0").replace(",", "") or 0)

                if pickup_match:
                    stops.append(
                        {
                            "stop_type": "pickup",
                            "address": _clean_addr(pickup_match.group(1).splitlines()[0]),
                            "pallets": pallets,
                            "weight_lbs": weight_lbs,
                        }
                    )
                if delivery_match:
                    stops.append(
                        {
                            "stop_type": "delivery",
                            "address": _clean_addr(delivery_match.group(1).splitlines()[0]),
                            "pallets": pallets,
                            "weight_lbs": weight_lbs,
                        }
                    )

        if stops:
            return stops

        # Generic one-off pickup/delivery patterns.
        pickup = re.search(r"pickup\s*:\s*(.+)", text, re.I)
        delivery = re.search(r"delivery\s*:\s*(.+)", text, re.I)
        pallets_match = re.search(r"pallets?\s*[:#]?\s*([\d,]+)", text, re.I)
        weight_match = re.search(r"weight\s*[:#]?\s*([\d,\.]+)\s*(?:lbs?|lb)?", text, re.I)
        pallets = int((pallets_match.group(1) if pallets_match else "0").replace(",", "") or 0)
        weight_lbs = float((weight_match.group(1) if weight_match else "0").replace(",", "") or 0)

        if pickup:
            stops.append(
                {
                    "stop_type": "pickup",
                    "address": _clean_addr(pickup.group(1)),
                    "pallets": pallets,
                    "weight_lbs": weight_lbs,
                }
            )
        if delivery:
            stops.append(
                {
                    "stop_type": "delivery",
                    "address": _clean_addr(delivery.group(1)),
                    "pallets": pallets,
                    "weight_lbs": weight_lbs,
                }
            )
        return stops

    def action_ai_calculate(self):
        self.ensure_one()

        # USE YOUR EXISTING SYSTEM PARAMETERS (NO CHANGE)
        openai_key = self.env["ir.config_parameter"].sudo().get_param("openai.api_key")
        mapbox_key = self.env["ir.config_parameter"].sudo().get_param("mapbox_api_key")

        if not openai_key:
            raise UserError("OpenAI API key missing.")
        if not mapbox_key:
            raise UserError("Mapbox API key missing.")

        email_text = self._clean_body()
        if not email_text:
            raise UserError("No email content found.")

        # ---------------- AI Extraction ----------------
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a senior freight dispatcher AI. Return ONLY valid JSON. "
                        "Ignore signatures and quoted thread history. Detect multi-load emails."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""
Extract all dispatch stops and load metrics from this customer email.

Return format:

{{
  "stops": [
    {{
      "stop_type": "pickup | delivery",
      "address": "full address",
      "pallets": number,
      "weight_lbs": number
    }}
  ],
  "inside_delivery": boolean,
  "liftgate": boolean,
  "detention_requested": boolean
}}

Rules:
- If the email has LOAD #1/LOAD #2 blocks, create pickup + delivery for EACH load.
- Extract pallets and weight for each load when available.
- If pallets missing but weight provided, estimate pallets as ceil(weight_lbs / 1800).
- Return valid JSON only.

Email:
{email_text}
"""
                }
            ],
            "temperature": 0.1
        }

        headers = {
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json"
        }

        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=60)

        data = response.json()

        if "choices" not in data:
            raise UserError(str(data))

        content = data["choices"][0]["message"]["content"]

        # FIX: robust JSON extraction
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise UserError("AI returned invalid JSON.")

        try:
            structured = json.loads(match.group(0))
        except json.JSONDecodeError:
            _logger.exception("AI returned malformed JSON, attempting fallback stop extraction")
            structured = {"stops": self._fallback_extract_stops(email_text)}

        stops = structured.get("stops") or self._fallback_extract_stops(email_text)

        for stop in stops:
            if not stop.get("pallets") and stop.get("weight_lbs"):
                stop["pallets"] = max(1, int(math.ceil(float(stop["weight_lbs"]) / 1800.0)))

        if not stops:
            raise UserError("No stops detected.")

        # ---------------- Create Stops ----------------
        self.dispatch_stop_ids.unlink()

        sequence = 1
        for stop in stops:
            self.env["premafirm.dispatch.stop"].create({
                "lead_id": self.id,
                "sequence": sequence,
                "stop_type": stop.get("stop_type"),
                "address": stop.get("address"),
                "pallets": stop.get("pallets", 0),
                "weight_lbs": stop.get("weight_lbs", 0),
            })
            sequence += 1

        # ---------------- Routing ----------------
        mapbox = MapboxService(self.env)
        ordered_stops = self.dispatch_stop_ids.sorted("sequence")

        total_distance = 0
        total_hours = 0

        for i in range(len(ordered_stops) - 1):
            route = mapbox.get_route(
                ordered_stops[i].address,
                ordered_stops[i + 1].address
            )

            ordered_stops[i + 1].write({
                "distance_km": route["distance_km"],
                "drive_hours": route["drive_hours"],
            })

            total_distance += route["distance_km"]
            total_hours += route["drive_hours"]

        self.total_distance_km = total_distance
        self.total_drive_hours = total_hours

        # ---------------- Pricing ----------------
        pricing = PricingEngine(self.env).calculate_pricing(self)

        self.write({
            "estimated_cost": pricing["estimated_cost"],
            "suggested_rate": pricing["suggested_rate"],
            "ai_recommendation": pricing["recommendation"],
            "inside_delivery": bool(structured.get("inside_delivery")),
            "liftgate": bool(structured.get("liftgate")),
            "detention_requested": bool(structured.get("detention_requested")),
        })

        # ---------------- Auto Suggested Reply ----------------
        reply_text = f"""
Hi {self.partner_name or ""},

Thank you for your request.

Total Distance: {total_distance:.1f} km
Estimated Drive Time: {total_hours:.2f} hrs

Our quoted rate: ${pricing['suggested_rate']:.0f}

Please let us know if you would like to proceed.

Best regards,
PremaFirm Logistics
"""

        # Draft response suggestion only; dispatcher manually reviews/edits before send.
        self.message_post(body=reply_text, subtype_xmlid="mail.mt_note")

        return True
