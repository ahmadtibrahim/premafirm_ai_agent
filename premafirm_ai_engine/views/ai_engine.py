import requests
import json
import re
from odoo import models, fields
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = "crm.lead"

    stop_ids = fields.One2many(
        "premafirm.load.stop",
        "lead_id",
        string="Stops"
    )

    def action_premafirm_ai_price(self):
        self.ensure_one()

        openai_key = self.env["ir.config_parameter"].sudo().get_param("openai.api_key")
        mapbox_token = self.env["ir.config_parameter"].sudo().get_param("mapbox.api.key")

        if not openai_key:
            raise UserError("OpenAI API key missing.")
        if not mapbox_token:
            raise UserError("Mapbox API key missing.")

        # -----------------------------
        # 1️⃣ Get latest email body
        # -----------------------------
        email_body = ""
        messages = self.message_ids.sorted("date", reverse=True)

        for msg in messages:
            if msg.body:
                email_body = re.sub("<.*?>", "", msg.body)
                break

        if not email_body:
            raise UserError("No email content found.")

        # -----------------------------
        # 2️⃣ Ask OpenAI for structured stops
        # -----------------------------
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
      "type": "pickup",
      "address": "City, Province",
      "pallets": 0,
      "weight_lbs": 0
    }}
  ]
}}

Email:
{email_body}
"""
                }
            ]
        }

        headers = {
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json"
        }

        ai_response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            data=json.dumps(payload),
            timeout=60
        )

        result = ai_response.json()

        if "choices" not in result:
            raise UserError("OpenAI error: " + str(result))

        content = result["choices"][0]["message"]["content"]

        try:
            structured = json.loads(content)
        except Exception:
            raise UserError("AI returned invalid JSON.")

        stops = structured.get("stops", [])

        if not stops:
            raise UserError("No stops detected.")

        # -----------------------------
        # 3️⃣ Clear old stops
        # -----------------------------
        self.stop_ids.unlink()

        sequence = 1
        addresses = []

        for stop in stops:
            record = self.env["premafirm.load.stop"].create({
                "lead_id": self.id,
                "sequence": sequence,
                "stop_type": stop.get("type"),
                "address": stop.get("address"),
                "pallets": stop.get("pallets", 0),
                "weight_lbs": stop.get("weight_lbs", 0),
            })

            addresses.append(stop.get("address"))
            sequence += 1

        # -----------------------------
        # 4️⃣ Route calculation
        # -----------------------------
        if len(addresses) >= 2:

            coords = []
            for addr in addresses:
                geo_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{addr}.json?access_token={mapbox_token}"
                geo_res = requests.get(geo_url).json()

                if geo_res.get("features"):
                    lon, lat = geo_res["features"][0]["center"]
                    coords.append(f"{lon},{lat}")

            if len(coords) >= 2:
                coord_string = ";".join(coords)

                route_url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{coord_string}?access_token={mapbox_token}"

                route_res = requests.get(route_url).json()

                if route_res.get("routes"):
                    meters = route_res["routes"][0]["distance"]
                    seconds = route_res["routes"][0]["duration"]

                    distance_km = meters / 1000
                    drive_hours = seconds / 3600

                    self.x_studio_distance_km = distance_km
                    self.x_studio_drive_hours = drive_hours

        return True
