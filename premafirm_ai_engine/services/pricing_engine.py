import json
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


class PricingEngine:
    def __init__(self, env):
        self.env = env
        self.rules = self._load_dispatch_rules()

    def _load_dispatch_rules(self):
        rules_path = Path(__file__).resolve().parents[1] / "data" / "dispatch_rules.json"
        default_rules = {
            "pricing": {
                "dry_rate_per_km": 2.25,
                "reefer_rate_per_km": 2.55,
                "min_load_charge": 450,
                "inside_delivery_fee": 125,
                "liftgate_fee": 85,
                "detention_per_hour": 75,
                "extra_stop_fee": 75,
                "overload_per_pallet": 12,
            },
            "costing": {
                "fuel_cost_per_km": 0.5,
                "maintenance_monthly": 1000,
                "default_monthly_km": 12000,
                "safety_cost_per_km": 0.6,
            },
            "traffic_rules": {"traffic_buffer_percent": 0.18},
            "hos_rules": {
                "canada_max_drive_hours": 13,
                "cross_border_max_drive_hours": 11,
                "max_on_duty_hours": 14,
                "pickup_hours": 1.0,
                "delivery_hours": 1.0,
                "extra_stop_hours": 0.75,
                "cross_border_buffer_hours": 2.0,
            },
            "dispatcher_limits": {
                "max_payload_lbs": 13000,
                "max_pallets": 12,
                "heavy_load_threshold_lbs": 11500,
                "deadhead_reject_percent": 0.4,
                "deadhead_score_thresholds": [0.15, 0.25, 0.35],
                "deadhead_unpaid_km_limit": 150,
                "overnight_cost_per_night": 130,
                "local_min_profit": 250,
                "regional_min_profit": 500,
                "longhaul_min_profit": 1200,
                "cross_border_min_rate_per_mile": 1.30,
            },
        }
        try:
            with rules_path.open("r", encoding="utf-8") as rules_file:
                loaded = json.load(rules_file)
            for section, values in default_rules.items():
                loaded.setdefault(section, {})
                for key, value in values.items():
                    loaded[section].setdefault(key, value)
            return loaded
        except Exception:
            _logger.exception("Failed to read dispatch rules JSON; using safe defaults")
            return default_rules

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
        multipliers = {
            "ftl_dry": 1.0,
            "ftl_reefer": 1.15,
            "ltl_dry": 0.9,
            "ltl_reefer": 1.0,
            "express": 1.35,
        }
        return base * multipliers.get(category_key, 1.0)


    @staticmethod
    def _extract_city(address):
        if not address:
            return ""
        return (address.split(",", 1)[0] or "").strip().lower()

    def _history_rate_adjustment(self, lead):
        """Return historical average rate for similar customer/route when available."""
        try:
            history_model = self.env["premafirm.pricing.history"]
        except Exception:
            return None

        if not getattr(lead, "partner_id", False):
            return None

        pickups = [s for s in lead.dispatch_stop_ids if s.stop_type == "pickup"]
        deliveries = [s for s in lead.dispatch_stop_ids if s.stop_type == "delivery"]
        pickup_city = self._extract_city(pickups[0].address if pickups else "")
        delivery_city = self._extract_city(deliveries[0].address if deliveries else "")
        if not pickup_city or not delivery_city:
            return None

        domain = [
            ("customer_id", "=", lead.partner_id.id),
            ("pickup_city", "ilike", pickup_city),
            ("delivery_city", "ilike", delivery_city),
            ("final_price", ">", 0),
        ]

        records = history_model.search(domain, order="sent_date desc", limit=20)
        if not records:
            return None

        similar = records.filtered(
            lambda r: (
                abs((r.distance_km or 0.0) - float(lead.total_distance_km or 0.0)) <= 80.0
                and abs((r.pallets or 0) - int(lead.total_pallets or 0)) <= 6
            )
        )
        sample = similar or records
        return sum(sample.mapped("final_price")) / max(1, len(sample))

    def calculate_pricing(self, lead):
        pricing_rules = self.rules["pricing"]
        costing_rules = self.rules["costing"]
        hos_rules = self.rules.get("hos_rules", {})
        limits = self.rules.get("dispatcher_limits", {})

        dispatch_stops = list(getattr(lead, "dispatch_stop_ids", []) or [])
        pickup_stops = [s for s in dispatch_stops if getattr(s, "stop_type", "") == "pickup"]
        delivery_stops = [s for s in dispatch_stops if getattr(s, "stop_type", "") == "delivery"]
        stop_count = len(dispatch_stops)
        extra_stops = max(0, stop_count - 2)

        loaded_km = float(getattr(lead, "total_distance_km", 0.0) or 0.0)
        deadhead_km = float(getattr(lead, "deadhead_km", 0.0) or 0.0)
        total_km = loaded_km + deadhead_km
        deadhead_percent = (deadhead_km / loaded_km) if loaded_km > 0 else 0.0

        fuel_cost_per_km = float(costing_rules.get("fuel_cost_per_km", 0.5))
        maintenance_monthly = float(costing_rules.get("maintenance_monthly", 1000))
        default_monthly_km = float(costing_rules.get("default_monthly_km", 12000))
        safety_cost_per_km = float(costing_rules.get("safety_cost_per_km", 0.6))
        maintenance_cost_per_km = maintenance_monthly / default_monthly_km if default_monthly_km else 0.0
        true_cost_per_km = max(fuel_cost_per_km + maintenance_cost_per_km, safety_cost_per_km)
        base_cost = total_km * true_cost_per_km

        vehicle = getattr(lead, "assigned_vehicle_id", None)
        max_payload_lbs = float(getattr(vehicle, "payload_limit_lbs", 0.0) or limits.get("max_payload_lbs", 13000))
        max_pallets = int(getattr(vehicle, "max_pallets", 0) or limits.get("max_pallets", 12))
        heavy_load_threshold_lbs = float(limits.get("heavy_load_threshold_lbs", 11500))
        load_weight_lbs = float(getattr(lead, "total_weight_lbs", 0.0) or 0.0)
        pallet_count = int(getattr(lead, "total_pallets", 0) or 0)
        heavy_load_flag = load_weight_lbs >= heavy_load_threshold_lbs

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
        pricing_model = (getattr(lead, "billing_mode", "per_km") or "per_km").upper()
        flat_rate = float(getattr(lead, "final_rate", 0.0) or getattr(lead, "suggested_rate", 0.0) or 0.0)
        rate_per_km = flat_rate if pricing_model == "PER_KM" and flat_rate else base_rate_per_km

        if pricing_model == "FLAT":
            gross_revenue = flat_rate or max(loaded_km * base_rate_per_km, pricing_rules["min_load_charge"])
        elif pricing_model == "PER_STOP":
            gross_revenue = float(pricing_rules.get("min_load_charge", 450)) + (float(pricing_rules.get("extra_stop_fee", 75)) * stop_count)
        else:
            gross_revenue = loaded_km * rate_per_km

        loaded_miles = loaded_km * 0.621371
        rate_per_mile = rate_per_km / 1.60934 if rate_per_km else 0.0
        if cross_border:
            gross_revenue = loaded_miles * rate_per_mile

        detention_hours = float(getattr(lead, "detention_hours", 0.0) or (1.0 if getattr(lead, "detention_requested", False) else 0.0))
        free_hours_per_stop = 2.0
        detention_rate_per_hour = float(pricing_rules.get("detention_per_hour", 75))
        detention_cost = max(0.0, detention_hours - free_hours_per_stop) * detention_rate_per_hour
        net_profit = gross_revenue - base_cost - overnight_cost - detention_cost

        zone = getattr(lead, "zone", False)
        if not zone:
            zone = "CROSS_BORDER" if cross_border else "GTA" if loaded_km <= 120 else "REGIONAL" if loaded_km <= 700 else "CROSS_COUNTRY"

        decision = None
        if load_weight_lbs > max_payload_lbs:
            decision = "REJECT_OVER_PAYLOAD"
        elif pallet_count > max_pallets:
            decision = "REJECT_OVER_PALLETS"
        elif deadhead_percent > float(limits.get("deadhead_reject_percent", 0.4)):
            decision = "REJECT_HIGH_DEADHEAD"
        elif deadhead_km > float(limits.get("deadhead_unpaid_km_limit", 150)) and bool(getattr(lead, "unpaid_deadhead", False)):
            decision = "REJECT_UNPAID_RETURN"
        elif net_profit < 0:
            decision = "REJECT_LOSS"
        elif zone == "GTA" and net_profit < float(limits.get("local_min_profit", 250)):
            decision = "REJECT_LOW_LOCAL"
        elif zone == "REGIONAL" and net_profit < float(limits.get("regional_min_profit", 500)):
            decision = "REJECT_LOW_REGIONAL"
        elif zone == "CROSS_COUNTRY" and net_profit < float(limits.get("longhaul_min_profit", 1200)):
            decision = "REJECT_LOW_LONGHAUL"
        elif zone == "CROSS_BORDER" and rate_per_mile < float(limits.get("cross_border_min_rate_per_mile", 1.3)):
            decision = "REJECT_LOW_RATE"

        score = 100
        if zone == "GTA" and rate_per_km < 1.55:
            score -= 25
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
        if stop_count > 5:
            score -= 10
        if net_profit > 1000:
            score += 5
        elif net_profit < 400:
            score -= 20

        if not decision:
            decision = "ACCEPT" if score >= 75 else "REVIEW" if score >= 60 else "REJECT"

        target_min_profit = float(limits.get("regional_min_profit", 500)) if zone != "GTA" else float(limits.get("local_min_profit", 250))
        suggested_rate = round(max(gross_revenue, base_cost + overnight_cost + detention_cost + target_min_profit), 0)
        estimated_cost = round(base_cost + overnight_cost + detention_cost, 0)

        recommendation = (
            f"Start-to-load deadhead {deadhead_km:.1f} km, loaded {loaded_km:.1f} km (total {total_km:.1f} km). "
            f"Gross ${gross_revenue:.0f}, cost ${estimated_cost:.0f}, net ${net_profit:.0f}. "
            f"Score {score} => {decision}."
        )

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
