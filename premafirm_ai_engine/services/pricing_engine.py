import json
from pathlib import Path


class PricingEngine:
    def __init__(self, env):
        self.env = env
        self.rules = self._load_dispatch_rules()

    def _load_dispatch_rules(self):
        rules_path = Path(__file__).resolve().parents[1] / "data" / "dispatch_rules.json"
        with rules_path.open("r", encoding="utf-8") as rules_file:
            return json.load(rules_file)

    def calculate_pricing(self, lead):
        service_type = "dry"
        for stop in lead.dispatch_stop_ids:
            if stop.service_type == "reefer":
                service_type = "reefer"
                break

        pricing_rules = self.rules["pricing"]
        costing_rules = self.rules["costing"]

        rate = (
            pricing_rules["reefer_rate_per_km"]
            if service_type == "reefer"
            else pricing_rules["dry_rate_per_km"]
        )

        base_price = lead.total_distance_km * rate
        suggested_rate = max(base_price, pricing_rules["min_load_charge"])

        estimated_cost = (
            (costing_rules["fuel_cost_per_km"] + costing_rules["maintenance_cost_per_km"])
            * lead.total_distance_km
            + costing_rules["insurance_daily"]
        )

        target_profit = costing_rules["target_net_profit_per_day"]
        if suggested_rate - estimated_cost < target_profit:
            suggested_rate = estimated_cost + target_profit

        recommendation = (
            f"Service type: {service_type}. "
            f"Estimated cost ${estimated_cost:.2f}, suggested rate ${suggested_rate:.2f}."
        )

        return {
            "estimated_cost": estimated_cost,
            "suggested_rate": suggested_rate,
            "recommendation": recommendation,
        }
