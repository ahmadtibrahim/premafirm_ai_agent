import requests
from odoo import models, fields
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = "crm.lead"

    stop_ids = fields.One2many(
        "premafirm.load.stop",
        "lead_id",
        string="Stops"
    )

    def _verify_required_field_configuration(self):
        """Validate critical Studio field mappings before running AI pricing."""
        field_model = self.env["ir.model.fields"].sudo()
        checks = [
            ("crm.lead", "x_studio_assigned_vehicle", "many2one", "fleet.vehicle"),
            ("crm.lead", "x_studio_suggested_rate", "float", None),
            ("crm.lead", "x_studio_target_profit", "float", None),
            ("crm.lead", "x_studio_distance_km", "float", None),
            ("crm.lead", "x_studio_drive_hours", "float", None),
            ("crm.lead", "x_studio_estimated_cost", "float", None),
            ("crm.lead", "x_studio_load_weight_lbs", "integer", None),
            ("crm.lead", "x_studio_pallet_count", "integer", None),
            ("fleet.vehicle", "x_studio_height_ft", "float", None),
            ("fleet.vehicle", "x_studio_x_is_busy", "boolean", None),
        ]

        for model_name, field_name, expected_type, expected_relation in checks:
            field = field_model.search([
                ("model", "=", model_name),
                ("name", "=", field_name),
            ], limit=1)
            if not field:
                raise UserError(f"Missing required field: {model_name}.{field_name}")
            if field.ttype != expected_type:
                raise UserError(
                    f"Invalid type for {model_name}.{field_name}. "
                    f"Expected {expected_type}, got {field.ttype}."
                )
            if expected_relation and field.relation != expected_relation:
                raise UserError(
                    f"Invalid relation for {model_name}.{field_name}. "
                    f"Expected {expected_relation}, got {field.relation}."
                )

    def _mapbox_geocode(self, address, mapbox_token):
        response = requests.get(
            f"https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json",
            params={"access_token": mapbox_token, "limit": 1},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        features = payload.get("features", [])
        if not features:
            raise UserError(f"Could not geocode address: {address}")

        feature = features[0]
        lon, lat = feature["center"]
        country = ""
        for context in feature.get("context", []):
            if str(context.get("id", "")).startswith("country"):
                country = context.get("short_code", "").upper()
                break
        return lon, lat, country

    def action_premafirm_ai_price(self):
        self.ensure_one()
        self._verify_required_field_configuration()

        mapbox_token = self.env["ir.config_parameter"].sudo().get_param("mapbox.api.key")
        diesel_price = float(
            self.env["ir.config_parameter"].sudo().get_param("premafirm_ai_engine.diesel_price", default="1.7")
        )
        fixed_costs = float(
            self.env["ir.config_parameter"].sudo().get_param("premafirm_ai_engine.fixed_costs", default="150")
        )

        if not mapbox_token:
            raise UserError("Mapbox API key missing.")

        vehicle = self.x_studio_assigned_vehicle
        if not vehicle:
            raise UserError("Assigned vehicle is required for AI pricing.")

        pickup_address = self.x_studio_char_field_8ci_1jh45n5oc
        delivery_address = self.x_studio_char_field_9da_1jhakip9j
        if not pickup_address or not delivery_address:
            raise UserError("Pickup and delivery addresses are required.")

        load_weight_lbs = self.x_studio_load_weight_lbs or 0
        pallet_count = self.x_studio_pallet_count or 0
        max_payload_lbs = vehicle.x_studio_max_payload_lbs or 0
        max_pallets = vehicle.x_studio_max_pallets_1 or 0
        current_load_lbs = vehicle.x_studio_current_load_lbs or 0

        remaining_payload = max_payload_lbs - current_load_lbs
        if hasattr(vehicle, "x_studio_remaining_payload_lbs"):
            vehicle.x_studio_remaining_payload_lbs = remaining_payload

        if pallet_count > max_pallets:
            self.x_studio_ai_recommendation = "EXCEEDS PALLET CAPACITY"
            return True

        if load_weight_lbs > max_payload_lbs:
            self.x_studio_ai_recommendation = "OVERWEIGHT"
            return True

        if vehicle.x_studio_x_is_busy:
            self.x_studio_ai_recommendation = "TRUCK NOT AVAILABLE"
            return True

        vehicle_height_ft = vehicle.x_studio_height_ft or 0.0
        vehicle_gvwr_lbs = vehicle.x_studio_gvwr_lbs or 0

        pickup_lon, pickup_lat, pickup_country = self._mapbox_geocode(pickup_address, mapbox_token)
        delivery_lon, delivery_lat, delivery_country = self._mapbox_geocode(delivery_address, mapbox_token)

        if pickup_country and delivery_country and pickup_country != delivery_country:
            self.x_studio_ai_recommendation = "TRUCK NOT AVAILABLE"
            raise UserError("Cross-border route detected; this lane is restricted by policy.")

        route_url = (
            "https://api.mapbox.com/directions/v5/mapbox/driving/"
            f"{pickup_lon},{pickup_lat};{delivery_lon},{delivery_lat}"
        )
        route_response = requests.get(
            route_url,
            params={
                "access_token": mapbox_token,
                "overview": "false",
                "exclude": "toll",
                # Traceability for truck constraints per spec.
                "metadata_height_ft": vehicle_height_ft,
                "metadata_gvwr_lbs": vehicle_gvwr_lbs,
            },
            timeout=30,
        )
        route_response.raise_for_status()
        route_payload = route_response.json()

        if not route_payload.get("routes"):
            raise UserError("No valid route found from Mapbox.")

        route = route_payload["routes"][0]
        distance_km = route["distance"] / 1000.0
        drive_hours = route["duration"] / 3600.0

        avg_mpg_loaded = vehicle.x_studio_avg_mpg_loaded or 0
        if avg_mpg_loaded <= 0:
            raise UserError("Vehicle average MPG loaded must be greater than zero.")

        fuel_used = distance_km / (avg_mpg_loaded * 1.609)
        fuel_cost = fuel_used * diesel_price

        target_profit = 400.0 * (drive_hours / 10.0)
        estimated_cost = fuel_cost + fixed_costs
        suggested_rate = estimated_cost + target_profit

        minimum_rate = estimated_cost + 400.0
        if suggested_rate < minimum_rate:
            suggested_rate = minimum_rate

        self.write({
            "x_studio_distance_km": distance_km,
            "x_studio_drive_hours": drive_hours,
            "x_studio_estimated_cost": estimated_cost,
            "x_studio_target_profit": target_profit,
            "x_studio_suggested_rate": suggested_rate,
            "x_studio_ai_recommendation": "OK TO BOOK",
        })

        return True
