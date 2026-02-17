import json
import math
from pathlib import Path


_RULES_CACHE = None


DEFAULT_RULES = {
    "base_location": "5585 McAdam Rd, Mississauga, ON",
    "target_profit_per_day": 400,
    "hos_rules": {
        "max_drive_hours_per_day": 13,
        "max_on_duty_hours_per_day": 14,
        "break_required_after_hours": 8,
    },
    "costs": {
        "fuel_per_km": 0.4,
        "driver_rate_per_hour": 30,
        "insurance_per_trip": 50,
        "toll_preference": "avoid",
    },
    "service_types": {
        "reefer": {"rate_multiplier": 1.15, "max_pallets": 26},
        "dry": {"rate_multiplier": 1.0, "max_pallets": 30},
    },
    "load_types": {
        "FTL": {"base_rate_multiplier": 1.0},
        "LTL": {"base_rate_multiplier": 0.8},
    },
    "vehicle_constraints": {"consider_height": True, "consider_weight": True},
}


def load_dispatch_rules(env=None):
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    rules_path = Path(__file__).resolve().parents[2] / "dispatch_rules.json"
    rules = DEFAULT_RULES.copy()

    if env:
        configured_path = env["ir.config_parameter"].sudo().get_param(
            "premafirm_ai_engine.dispatch_rules_path"
        )
        if configured_path:
            rules_path = Path(configured_path)

    if rules_path.exists():
        with rules_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        rules = _merge_rules(DEFAULT_RULES, loaded)

    _RULES_CACHE = rules
    return rules


def _merge_rules(defaults, incoming):
    merged = dict(defaults)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_rules(merged[key], value)
        else:
            merged[key] = value
    return merged


class DispatchService:
    def __init__(self, env):
        self.env = env

    def compute_lead_totals(self, lead):
        rules = load_dispatch_rules(self.env)
        stops = lead.stop_ids.sorted("sequence")
        vehicle = lead.x_studio_assigned_vehicle

        if not vehicle or not stops:
            return self._empty_result("Assign vehicle and at least one stop to compute totals.")

        base_location = vehicle.x_studio_location or rules["base_location"]
        ordered_addresses = [base_location] + [s.address for s in stops if s.address]
        leg_count = max(len(ordered_addresses) - 1, 0)

        # Placeholder route math until external map routing integration is implemented.
        distance_km = leg_count * 125.0
        drive_hours = distance_km / 70.0 if distance_km else 0.0

        pallets = sum(stops.mapped("pallet_count"))
        weight_lbs = sum(stops.mapped("load_weight_lbs"))

        service_type = self._resolve_service_type(stops, vehicle)
        load_type = self._resolve_load_type(stops, vehicle)

        cost_rules = rules["costs"]
        estimated_cost = (
            distance_km * cost_rules["fuel_per_km"]
            + drive_hours * cost_rules["driver_rate_per_hour"]
            + cost_rules["insurance_per_trip"]
        )

        days = max(1, math.ceil(drive_hours / rules["hos_rules"]["max_drive_hours_per_day"]))
        target_profit = days * rules["target_profit_per_day"]

        service_multiplier = rules["service_types"].get(service_type, {}).get("rate_multiplier", 1.0)
        load_multiplier = rules["load_types"].get(load_type, {}).get("base_rate_multiplier", 1.0)

        suggested_rate = (estimated_cost + target_profit) * service_multiplier * load_multiplier

        total_pickup_dt = min((s.stop_pickup_dt for s in stops if s.stop_pickup_dt), default=False)
        total_delivery_dt = max((s.stop_delivery_dt for s in stops if s.stop_delivery_dt), default=False)

        recommendation = self._build_recommendation(
            pallets=pallets,
            weight_lbs=weight_lbs,
            service_type=service_type,
            load_type=load_type,
            distance_km=distance_km,
            drive_hours=drive_hours,
        )

        return {
            "distance_km": distance_km,
            "drive_hours": drive_hours,
            "estimated_cost": estimated_cost,
            "target_profit": target_profit,
            "suggested_rate": suggested_rate,
            "ai_recommendation": recommendation,
            "service_type": service_type,
            "load_type": load_type,
            "total_pickup_dt": total_pickup_dt,
            "total_delivery_dt": total_delivery_dt,
            "aggregated_pallet_count": pallets,
            "aggregated_load_weight_lbs": weight_lbs,
        }

    def _resolve_service_type(self, stops, vehicle):
        override = next((s.service_type for s in stops if s.service_type), False)
        return override or vehicle.x_studio_service_type or "dry"

    def _resolve_load_type(self, stops, vehicle):
        override = next((s.load_type for s in stops if s.load_type), False)
        return override or vehicle.x_studio_load_type or "FTL"

    def _empty_result(self, recommendation):
        return {
            "distance_km": 0.0,
            "drive_hours": 0.0,
            "estimated_cost": 0.0,
            "target_profit": 0.0,
            "suggested_rate": 0.0,
            "ai_recommendation": recommendation,
            "service_type": False,
            "load_type": False,
            "total_pickup_dt": False,
            "total_delivery_dt": False,
            "aggregated_pallet_count": 0,
            "aggregated_load_weight_lbs": 0.0,
        }

    def _build_recommendation(self, pallets, weight_lbs, service_type, load_type, distance_km, drive_hours):
        return (
            f"Route ready. {distance_km:.1f} km / {drive_hours:.1f} h. "
            f"Use {service_type.upper()} service with {load_type}. "
            f"Load: {pallets} pallets, {weight_lbs} lbs."
        )
