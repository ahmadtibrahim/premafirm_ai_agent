import json
import math
from pathlib import Path

_RULES_CACHE = None

DEFAULT_RULES = {
    "base_location": "5585 McAdam Rd, Mississauga, ON",
    "target_profit_per_day": 400,
    "hos_rules": {
        "max_drive_hours_per_day": 13,
    },
    "costs": {
        "fuel_per_km": 0.4,
        "driver_rate_per_hour": 30,
        "insurance_per_trip": 50,
    },
    "service_types": {
        "reefer": {"rate_multiplier": 1.15},
        "dry": {"rate_multiplier": 1.0},
    },
    "load_types": {
        "FTL": {"base_rate_multiplier": 1.0},
        "LTL": {"base_rate_multiplier": 0.8},
    },
}


def load_dispatch_rules(env=None):
    global _RULES_CACHE
    if _RULES_CACHE:
        return _RULES_CACHE

    rules = DEFAULT_RULES.copy()
    rules_path = Path(__file__).resolve().parents[2] / "dispatch_rules.json"

    if rules_path.exists():
        with rules_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        rules.update(loaded)

    _RULES_CACHE = rules
    return rules


class DispatchService:

    def __init__(self, env):
        self.env = env

    def compute_lead_totals(self, lead):
        rules = load_dispatch_rules(self.env)

        stops = lead.stop_ids.sorted("sequence")
        vehicle = lead.x_studio_assigned_vehicle

        if not vehicle or not stops:
            return self._empty("Assign vehicle and stops.")

        base_location = vehicle.x_studio_location or rules["base_location"]
        addresses = [base_location] + [s.address for s in stops if s.address]

        leg_count = max(len(addresses) - 1, 0)
        distance_km = leg_count * 125.0
        drive_hours = distance_km / 70.0 if distance_km else 0.0

        pallets = sum(stops.mapped("pallets"))
        weight_lbs = sum(stops.mapped("weight_lbs"))

        service_type = vehicle.x_studio_service_type or "dry"
        load_type = vehicle.x_studio_load_type or "FTL"

        estimated_cost = (
            distance_km * rules["costs"]["fuel_per_km"]
            + drive_hours * rules["costs"]["driver_rate_per_hour"]
            + rules["costs"]["insurance_per_trip"]
        )

        days = max(1, math.ceil(drive_hours / rules["hos_rules"]["max_drive_hours_per_day"]))
        target_profit = days * rules["target_profit_per_day"]

        service_multiplier = rules["service_types"][service_type]["rate_multiplier"]
        load_multiplier = rules["load_types"][load_type]["base_rate_multiplier"]

        suggested_rate = (estimated_cost + target_profit) * service_multiplier * load_multiplier

        total_pickup_dt = min(
            (s.x_studio_pickup_dt for s in stops if s.x_studio_pickup_dt),
            default=False,
        )

        total_delivery_dt = max(
            (s.x_studio_delivery_dt for s in stops if s.x_studio_delivery_dt),
            default=False,
        )

        return {
            "x_studio_distance_km": distance_km,
            "x_studio_drive_hours": drive_hours,
            "x_studio_estimated_cost": estimated_cost,
            "x_studio_target_profit": target_profit,
            "x_studio_suggested_rate": suggested_rate,
            "x_studio_ai_recommendation": f"{distance_km:.1f} km | {pallets} pallets",
            "x_studio_service_type": service_type,
            "x_studio_load_type": load_type,
            "x_studio_total_pickup_dt": total_pickup_dt,
            "x_studio_total_delivery_dt": total_delivery_dt,
            "x_studio_aggregated_pallet_count": pallets,
            "x_studio_aggregated_load_weight_lbs": weight_lbs,
        }

    def _empty(self, message):
        return {
            "x_studio_distance_km": 0.0,
            "x_studio_drive_hours": 0.0,
            "x_studio_estimated_cost": 0.0,
            "x_studio_target_profit": 0.0,
            "x_studio_suggested_rate": 0.0,
            "x_studio_ai_recommendation": message,
            "x_studio_service_type": False,
            "x_studio_load_type": False,
            "x_studio_total_pickup_dt": False,
            "x_studio_total_delivery_dt": False,
            "x_studio_aggregated_pallet_count": 0,
            "x_studio_aggregated_load_weight_lbs": 0.0,
        }
