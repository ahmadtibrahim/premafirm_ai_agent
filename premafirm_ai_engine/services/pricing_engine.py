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
                "fuel_cost_per_km": 0.55,
                "maintenance_cost_per_km": 0.2,
                "insurance_daily": 95,
                "target_net_profit_per_day": 400,
                "overhead_daily": 60,
            },
            "traffic_rules": {"traffic_buffer_percent": 0.18},
            "hos_rules": {"max_drive_hours": 13},
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
        traffic_buffer_percent = float(self.rules.get("traffic_rules", {}).get("traffic_buffer_percent", 0.0))

        product_category = self._resolve_product_category_key(lead)
        base_rate = self._resolve_base_rate(pricing_rules, product_category)

        buffered_distance = float(lead.total_distance_km or 0.0) * (1.0 + traffic_buffer_percent)
        base_price = buffered_distance * base_rate
        base_price = max(base_price, pricing_rules["min_load_charge"])

        operating_cost = (
            (costing_rules["fuel_cost_per_km"] + costing_rules["maintenance_cost_per_km"]) * buffered_distance
            + costing_rules["insurance_daily"]
            + costing_rules["overhead_daily"]
        )

        pickup_stops = len([s for s in lead.dispatch_stop_ids if s.stop_type == "pickup"])
        delivery_stops = len([s for s in lead.dispatch_stop_ids if s.stop_type == "delivery"])
        extra_stop_count = max(0, pickup_stops + delivery_stops - 2)
        if extra_stop_count:
            base_price += extra_stop_count * pricing_rules["extra_stop_fee"]

        if (lead.total_pallets or 0) > 12:
            base_price += (lead.total_pallets - 12) * pricing_rules["overload_per_pallet"]

        if lead.liftgate:
            base_price += pricing_rules["liftgate_fee"]
        if lead.inside_delivery:
            base_price += pricing_rules["inside_delivery_fee"]
        if lead.detention_requested:
            base_price += pricing_rules["detention_per_hour"]

        target_profit = costing_rules["target_net_profit_per_day"]
        if base_price - operating_cost < target_profit:
            base_price = operating_cost + target_profit

        history_rate = self._history_rate_adjustment(lead)
        if history_rate:
            # Blend AI/rule output with accepted historical pricing for consistency.
            base_price = (base_price * 0.35) + (history_rate * 0.65)

        suggested_rate = round(base_price, 0)
        estimated_cost = round(operating_cost, 0)

        hos_limit = float(self.rules.get("hos_rules", {}).get("max_drive_hours", 13))
        warnings = []
        if float(lead.total_drive_hours or 0.0) > hos_limit:
            warnings.append(
                f"Estimated drive time {lead.total_drive_hours:.2f}h exceeds {hos_limit:.0f}h HOS limit; consider team or relay planning."
            )

        recommendation = (
            f"Distance {lead.total_distance_km:.1f} km (+{traffic_buffer_percent*100:.0f}% traffic buffer -> {buffered_distance:.1f} km). "
            f"Estimated drive {lead.total_drive_hours:.2f} hrs. Estimated operating cost ${estimated_cost:.0f}. "
            f"Recommended sell rate ${suggested_rate:.0f} to protect minimum ${target_profit:.0f} daily net target."
        )
        if history_rate:
            recommendation += f" Historical average for similar customer/route: ${history_rate:.0f}."
        if warnings:
            recommendation += " " + " ".join(warnings)

        return {
            "estimated_cost": estimated_cost,
            "suggested_rate": suggested_rate,
            "recommendation": recommendation,
            "warnings": warnings,
            "service_type": product_category,
        }
