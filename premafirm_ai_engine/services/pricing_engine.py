import logging
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_logger = logging.getLogger(__name__)

try:
    from .dispatch_rules_engine import DispatchRulesEngine
except Exception:
    module_path = Path(__file__).resolve().parent / "dispatch_rules_engine.py"
    spec = spec_from_file_location("dispatch_rules_engine", module_path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    DispatchRulesEngine = module.DispatchRulesEngine


class PricingEngine:
    def __init__(self, env):
        self.env = env
        self.rules = DispatchRulesEngine(env).rules

    def _load_dispatch_rules(self):
        return DispatchRulesEngine(self.env).rules

    @staticmethod
    def _resolve_product_category_key(lead):
        product = getattr(lead, "product_id", None)
        category = (product.categ_id.name if product and getattr(product, "categ_id", None) else "").strip().lower()
        if category in {"ftl dry", "dry"}:
            return "ftl_dry"
        if category in {"ftl reefer", "reefer"}:
            return "ftl_reefer"
        if category == "ltl dry":
            return "ltl_dry"
        if category == "ltl reefer":
            return "ltl_reefer"
        if category == "express":
            return "express"
        return "ftl_dry"

    @staticmethod
    def _resolve_base_rate(pricing_rules, category_key):
        base = float(pricing_rules.get("dry_rate_per_km", 2.25))
        multipliers = {"ftl_dry": 1.0, "ftl_reefer": 1.15, "ltl_dry": 0.9, "ltl_reefer": 1.0, "express": 1.35}
        return base * multipliers.get(category_key, 1.0)

    @staticmethod
    def _extract_city(address):
        if not address:
            return ""
        return (address.split(",", 1)[0] or "").strip().lower()

    def _history_rate_adjustment(self, lead):
        return None

    def calculate_pricing(self, lead):
        pricing_rules = self.rules["pricing"]
        costing_rules = self.rules["costing"]
        hos_rules = self.rules.get("hos_rules", {})
        limits = self.rules.get("dispatcher_limits", {})

        dispatch_stops = list(getattr(lead, "dispatch_stop_ids", []) or [])
        stop_count = len(dispatch_stops)
        extra_stops = max(0, stop_count - 2)

        loaded_km = float(getattr(lead, "total_distance_km", 0.0) or 0.0)
        deadhead_km = float(getattr(lead, "deadhead_km", 0.0) or 0.0)
        total_km = loaded_km + deadhead_km
        deadhead_percent = (deadhead_km / total_km) if total_km else 0.0

        vehicle = getattr(lead, "assigned_vehicle_id", None)
        load_weight_lbs = float(getattr(lead, "total_weight_lbs", 0.0) or 0.0)
        pallet_count = int(getattr(lead, "total_pallets", 0) or 0)
        max_payload_lbs = float(getattr(vehicle, "payload_limit_lbs", 0.0) or limits.get("max_payload_lbs", 13000))
        max_pallets = int(getattr(vehicle, "max_pallets", 0) or limits.get("max_pallets", 12))
        heavy_load_flag = load_weight_lbs >= float(limits.get("heavy_load_threshold_lbs", 11500))

        fuel_cost = total_km * float(costing_rules.get("fuel_cost_per_km", 0.5))
        maintenance_cost = total_km * 0.22
        base_cost = fuel_cost + maintenance_cost

        cross_border = any((getattr(s, "country", "") or "").upper() in {"US", "USA", "UNITED STATES"} for s in dispatch_stops)
        drive_hours = total_km / 85.0 if total_km else 0.0
        non_drive_time = float(hos_rules.get("pickup_hours", 1.0)) + float(hos_rules.get("delivery_hours", 1.0))
        non_drive_time += float(hos_rules.get("extra_stop_hours", 0.75)) * extra_stops
        if cross_border:
            max_drive_hours = float(hos_rules.get("cross_border_max_drive_hours", 11))
            non_drive_time += float(hos_rules.get("cross_border_buffer_hours", 2.0))
        else:
            max_drive_hours = float(hos_rules.get("canada_max_drive_hours", 13))
        total_on_duty = drive_hours + non_drive_time
        max_on_duty_hours = float(hos_rules.get("max_on_duty_hours", 14))
        overnight_required = drive_hours > max_drive_hours or total_on_duty > max_on_duty_hours
        nights_required = int(drive_hours // max_drive_hours) if max_drive_hours else 0
        overnight_cost = nights_required * float(limits.get("overnight_cost_per_night", 130))

        product_category = self._resolve_product_category_key(lead)
        base_rate_per_km = self._resolve_base_rate(pricing_rules, product_category)
        flat_rate = float(getattr(lead, "final_rate", 0.0) or getattr(lead, "suggested_rate", 0.0) or 0.0)
        rate_per_km = base_rate_per_km

        # flat mode assumed by default
        gross_revenue = flat_rate or max(loaded_km * rate_per_km, pricing_rules["min_load_charge"])

        detention_hours = float(getattr(lead, "detention_hours", 0.0) or (1.0 if getattr(lead, "detention_requested", False) else 0.0))
        detention_cost = max(0.0, detention_hours - 2.0) * float(pricing_rules.get("detention_per_hour", 75))
        net_profit = gross_revenue - base_cost - overnight_cost - detention_cost

        zone = getattr(lead, "zone", False) or ("CROSS_BORDER" if cross_border else "GTA" if loaded_km <= 120 else "REGIONAL" if loaded_km <= 700 else "CROSS_COUNTRY")
        decision = None
        if load_weight_lbs > max_payload_lbs:
            decision = "REJECT_OVER_PAYLOAD"
        elif pallet_count > max_pallets:
            decision = "REJECT_OVER_PALLETS"
        elif deadhead_percent > float(limits.get("deadhead_reject_percent", 0.4)):
            decision = "REJECT_HIGH_DEADHEAD"
        elif net_profit < 0:
            decision = "REJECT_LOSS"

        score = 100
        if deadhead_percent > 0.35:
            score -= 25
        elif deadhead_percent > 0.25:
            score -= 15
        elif deadhead_percent > 0.15:
            score -= 5
        if overnight_required:
            score -= 15
        if heavy_load_flag:
            score -= 10

        if not decision:
            decision = "ACCEPT" if score >= 75 else "REVIEW" if score >= 60 else "REJECT"

        target_min_profit = float(limits.get("regional_min_profit", 500)) if zone != "GTA" else float(limits.get("local_min_profit", 250))
        suggested_rate = round(max(gross_revenue, base_cost + overnight_cost + detention_cost + target_min_profit), 0)
        estimated_cost = round(base_cost + overnight_cost + detention_cost, 0)

        recommendation = f"Start-to-load deadhead {deadhead_km:.1f} km, loaded {loaded_km:.1f} km (total {total_km:.1f} km). Gross ${gross_revenue:.0f}, cost ${estimated_cost:.0f}, net ${net_profit:.0f}. Score {score} => {decision}."

        return {
            "estimated_cost": estimated_cost,
            "suggested_rate": suggested_rate,
            "recommendation": recommendation,
            "warnings": [],
            "service_type": product_category,
            "start_location": getattr(vehicle, "home_location", False) if vehicle else getattr(lead, "manual_origin", False),
            "deadhead_km": round(deadhead_km, 2),
            "total_km": round(total_km, 2),
            "gross_revenue": round(gross_revenue, 2),
            "base_cost": round(base_cost, 2),
            "overnight_cost": round(overnight_cost, 2),
            "detention_cost": round(detention_cost, 2),
            "NET_profit": round(net_profit, 2),
            "score": score,
            "decision": decision,
        }
