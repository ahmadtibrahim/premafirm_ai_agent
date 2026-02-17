import base64
import json
import re

import requests

from odoo import models
from odoo.tools import html2plaintext

from ..services.mapbox_service import MapboxService
from ..services.pricing_engine import PricingEngine


class CrmLeadAiEngine(models.Model):
    _inherit = "crm.lead"

    def _clean_html(self, text):
        if not text:
            return ""
        return html2plaintext(text)

    def _extract_with_openai(self, payload_text):
        api_key = self.env["ir.config_parameter"].sudo().get_param("openai_api_key")
        if not api_key:
            return {"stops": [], "summary": "OpenAI API key is not configured."}

        prompt = (
            "Extract structured dispatch data from the following communication. "
            "Return valid JSON only with shape: "
            "{\"stops\":[{\"stop_type\":\"pickup|delivery\",\"address\":\"...\","
            "\"pallets\":number,\"weight_lbs\":number,\"service_type\":\"dry|reefer\","
            "\"pickup_datetime_est\":\"YYYY-MM-DD HH:MM:SS\","
            "\"delivery_datetime_est\":\"YYYY-MM-DD HH:MM:SS\"}],"
            "\"summary\":\"...\"}.\n\n"
            f"CONTENT:\n{payload_text}"
        )

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=45,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {"stops": [], "summary": "No structured response received from AI."}
        return json.loads(match.group(0))

    def action_ai_calculate(self):
        mapbox = MapboxService(self.env)
        pricing_engine = PricingEngine(self.env)

        for lead in self:
            compiled_chunks = []
            for message in lead.message_ids:
                body = self._clean_html(message.body or "")
                subject = message.subject or ""
                sender = message.email_from or ""

                attachment_text = []
                for attachment in message.attachment_ids:
                    decoded = base64.b64decode(attachment.datas or b"")
                    attachment_text.append(decoded.decode("utf-8", errors="ignore"))

                compiled_chunks.append(
                    "\n".join([
                        f"Subject: {subject}",
                        f"Sender: {sender}",
                        f"Body: {body}",
                        "Attachments:",
                        "\n".join(attachment_text),
                    ])
                )

            extracted = self._extract_with_openai("\n\n".join(compiled_chunks))
            lead.dispatch_stop_ids.unlink()

            created_stops = self.env["premafirm.dispatch.stop"]
            for index, stop_data in enumerate(extracted.get("stops", []), start=1):
                created_stops |= self.env["premafirm.dispatch.stop"].create({
                    "lead_id": lead.id,
                    "sequence": index,
                    "stop_type": stop_data.get("stop_type"),
                    "address": stop_data.get("address"),
                    "pallets": stop_data.get("pallets", 0),
                    "weight_lbs": stop_data.get("weight_lbs", 0.0),
                    "service_type": stop_data.get("service_type"),
                    "pickup_datetime_est": stop_data.get("pickup_datetime_est"),
                    "delivery_datetime_est": stop_data.get("delivery_datetime_est"),
                })

            sorted_stops = created_stops.sorted("sequence")
            for idx in range(len(sorted_stops) - 1):
                origin = sorted_stops[idx]
                destination = sorted_stops[idx + 1]
                route = mapbox.get_route(origin.address, destination.address)
                origin.write({
                    "distance_km": route["distance_km"],
                    "drive_hours": route["drive_hours"],
                    "map_url": (
                        "https://www.mapbox.com/directions/?"
                        f"origin={origin.address}&destination={destination.address}"
                    ),
                })

            lead._compute_dispatch_totals()
            pricing = pricing_engine.calculate_pricing(lead)
            recommendation = extracted.get("summary") or pricing["recommendation"]

            lead.write({
                "estimated_cost": pricing["estimated_cost"],
                "suggested_rate": pricing["suggested_rate"],
                "ai_recommendation": recommendation,
            })

        return True
