import json
import re
import requests
from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.mapbox_service import MapboxService
from ..services.pricing_engine import PricingEngine


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _clean_body(self):
        messages = self.message_ids.sorted("date", reverse=True)
        for msg in messages:
            if msg.body:
                return html2plaintext(msg.body)
        return ""

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
                    "content": "Return ONLY valid JSON."
                },
                {
                    "role": "user",
                    "content": f"""
Extract pickup and delivery stops.

Return format:

{{
  "stops": [
    {{
      "stop_type": "pickup | delivery",
      "address": "full address",
      "pallets": number,
      "weight_lbs": number
    }}
  ]
}}

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

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )

        data = response.json()

        if "choices" not in data:
            raise UserError(str(data))

        content = data["choices"][0]["message"]["content"]

        # FIX: robust JSON extraction
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise UserError("AI returned invalid JSON.")

        structured = json.loads(match.group(0))
        stops = structured.get("stops", [])

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

        self.message_post(body=reply_text)

        return True
