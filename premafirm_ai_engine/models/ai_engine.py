import base64
import json
import re
from datetime import datetime, timedelta

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

    def _safe_datetime(self, value):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _extract_with_openai(self, payload_text):
        api_key = self.env["ir.config_parameter"].sudo().get_param("openai_api_key")
        if not api_key:
            return {"loads": [], "special_services": {}}

        system_prompt = (
            "You are a freight dispatch AI for a Canadian LTL/FTL trucking company. "
            "Extract structured logistics data from raw email text or PDF text. "
            "Return ONLY valid JSON. No explanation. No markdown.\n\n"
            "If multiple loads exist, return them as separate load objects. "
            "If one pickup with multiple deliveries, create multiple stop objects. "
            "If data missing, return null for that field.\n\n"
            "Expected JSON format:\n"
            "{\n"
            "  \"loads\": [\n"
            "    {\n"
            "      \"service_type\": \"dry | reefer\",\n"
            "      \"total_weight_lbs\": number,\n"
            "      \"stops\": [\n"
            "        {\n"
            "          \"stop_type\": \"pickup | delivery\",\n"
            "          \"address\": \"full address string\",\n"
            "          \"pallets\": number,\n"
            "          \"weight_lbs\": number,\n"
            "          \"requested_datetime\": \"YYYY-MM-DD HH:MM or null\",\n"
            "          \"time_window_start\": \"YYYY-MM-DD HH:MM or null\",\n"
            "          \"time_window_end\": \"YYYY-MM-DD HH:MM or null\"\n"
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ],\n"
            "  \"special_services\": {\n"
            "    \"inside_delivery\": true | false,\n"
            "    \"liftgate\": true | false,\n"
            "    \"detention_requested\": true | false\n"
            "  }\n"
            "}\n\n"
            "Rules:\n"
            "- Convert \"Same Day\" to null datetime but keep note in comment field if needed.\n"
            "- If only city + postal given, keep as-is.\n"
            "- If pallets total provided but not per stop, divide logically if possible.\n"
            "- Weight must be numeric (lbs only).\n"
            "- Detect service type from words like:\n"
            "  - Reefer, Temperature, Frozen → reefer\n"
            "  - Otherwise → dry"
        )

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"<<Insert cleaned email body + extracted PDF text here>>\n\n{payload_text}",
                    },
                ],
                "temperature": 0.1,
            },
            timeout=45,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {"loads": [], "special_services": {}}
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

            loads = extracted.get("loads", [])
            special_services = extracted.get("special_services") or {}

            created_stops = self.env["premafirm.dispatch.stop"]
            sequence = 1
            for load in loads:
                stops = load.get("stops", [])
                for stop_data in stops:
                    created_stops |= self.env["premafirm.dispatch.stop"].create({
                        "lead_id": lead.id,
                        "sequence": sequence,
                        "stop_type": stop_data.get("stop_type"),
                        "address": stop_data.get("address"),
                        "pallets": stop_data.get("pallets") or 0,
                        "weight_lbs": stop_data.get("weight_lbs") or 0.0,
                        "service_type": load.get("service_type") or "dry",
                        "requested_datetime": stop_data.get("requested_datetime"),
                        "pickup_window_start": stop_data.get("time_window_start") if stop_data.get("stop_type") == "pickup" else None,
                        "pickup_window_end": stop_data.get("time_window_end") if stop_data.get("stop_type") == "pickup" else None,
                        "delivery_window_start": stop_data.get("time_window_start") if stop_data.get("stop_type") == "delivery" else None,
                        "delivery_window_end": stop_data.get("time_window_end") if stop_data.get("stop_type") == "delivery" else None,
                    })
                    sequence += 1

            sorted_stops = created_stops.sorted("sequence")
            vehicle_start = lead.assigned_vehicle_id.x_studio_location if lead.assigned_vehicle_id else None
            work_start_hour = lead.assigned_vehicle_id.vehicle_work_start_time if lead.assigned_vehicle_id else 8.0
            now = datetime.now().replace(second=0, microsecond=0)
            start_hour = int(work_start_hour)
            start_minutes = int((work_start_hour - start_hour) * 60)
            departure_at = now.replace(hour=start_hour, minute=start_minutes)

            accumulated_drive_today = 0.0
            total_adjusted_drive = 0.0
            warning_messages = []

            for idx, destination in enumerate(sorted_stops):
                if idx == 0:
                    origin_address = vehicle_start or destination.address
                else:
                    origin_address = sorted_stops[idx - 1].address

                route = mapbox.get_route(origin_address, destination.address)
                adjusted_drive = route["drive_hours"] * 1.18

                if accumulated_drive_today + adjusted_drive > 13:
                    departure_at = (departure_at + timedelta(days=1)).replace(hour=start_hour, minute=start_minutes)
                    accumulated_drive_today = 0.0

                arrival_at = departure_at + timedelta(hours=adjusted_drive)
                accumulated_drive_today += adjusted_drive
                total_adjusted_drive += adjusted_drive

                if accumulated_drive_today > 4:
                    arrival_at += timedelta(minutes=15)
                if accumulated_drive_today > 8:
                    arrival_at += timedelta(minutes=30)

                window_start = self._safe_datetime(destination.pickup_window_start if destination.stop_type == "pickup" else destination.delivery_window_start)
                window_end = self._safe_datetime(destination.pickup_window_end if destination.stop_type == "pickup" else destination.delivery_window_end)

                if window_start and arrival_at < window_start:
                    arrival_at = window_start

                if window_end and arrival_at > window_end:
                    warning_messages.append(
                        f"Stop {destination.sequence} arrival {arrival_at.strftime('%Y-%m-%d %H:%M')} exceeds time window."
                    )

                write_vals = {
                    "distance_km": route["distance_km"],
                    "drive_hours": adjusted_drive,
                    "map_url": (
                        "https://www.mapbox.com/directions/?"
                        f"origin={origin_address}&destination={destination.address}"
                    ),
                }
                if destination.stop_type == "pickup":
                    write_vals["pickup_datetime_est"] = arrival_at.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    write_vals["delivery_datetime_est"] = arrival_at.strftime("%Y-%m-%d %H:%M:%S")
                destination.write(write_vals)
                departure_at = arrival_at

            lead._compute_dispatch_totals()
            pricing = pricing_engine.calculate_pricing(lead)

            recommendation = (
                "Estimated total distance: "
                f"{lead.total_distance_km:.1f} km.\n"
                f"Estimated driving time: {total_adjusted_drive:.2f} hrs.\n"
                f"Operating cost approx: ${pricing['estimated_cost']:.0f}.\n"
                "To maintain minimum $400 daily net target, "
                f"suggested rate: ${pricing['suggested_rate']:.0f}."
            )
            if warning_messages:
                recommendation = recommendation + "\nWarnings: " + " ".join(warning_messages)

            lead.write({
                "inside_delivery": bool(special_services.get("inside_delivery")),
                "liftgate": bool(special_services.get("liftgate")),
                "detention_requested": bool(special_services.get("detention_requested")),
                "estimated_cost": pricing["estimated_cost"],
                "suggested_rate": pricing["suggested_rate"],
                "ai_recommendation": recommendation,
            })

        return True
