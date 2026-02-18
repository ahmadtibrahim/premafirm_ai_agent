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

        base_rate = (
            pricing_rules["reefer_rate_per_km"]
            if service_type == "reefer"
            else pricing_rules["dry_rate_per_km"]
        )

        base_price = lead.total_distance_km * base_rate
        if base_price < pricing_rules["min_load_charge"]:
            base_price = pricing_rules["min_load_charge"]

        operating_cost = (
            (costing_rules["fuel_cost_per_km"] + costing_rules["maintenance_cost_per_km"])
            * lead.total_distance_km
            + costing_rules["insurance_daily"]
            + costing_rules["overhead_daily"]
        )

        required_profit_target = costing_rules["target_net_profit_per_day"]
        if base_price - operating_cost < required_profit_target:
            base_price = operating_cost + required_profit_target

        delivery_stops = len([s for s in lead.dispatch_stop_ids if s.stop_type == "delivery"])
        if delivery_stops > 1:
            base_price += (delivery_stops - 1) * pricing_rules["extra_stop_fee"]

        if lead.total_pallets > 12:
            base_price += (lead.total_pallets - 12) * pricing_rules["overload_per_pallet"]

        if lead.liftgate:
            base_price += pricing_rules["liftgate_fee"]

        if lead.inside_delivery:
            base_price += pricing_rules["inside_delivery_fee"]

        if lead.detention_requested:
            base_price += pricing_rules["detention_per_hour"]

        suggested_rate = round(base_price, 0)
        estimated_cost = round(operating_cost, 0)

        recommendation = (
            f"Estimated total distance: {lead.total_distance_km:.1f} km. "
            f"Estimated driving time: {lead.total_drive_hours:.2f} hrs. "
            f"Operating cost approx: ${estimated_cost:.0f}. "
            "To maintain minimum $400 daily net target, "
            f"suggested rate: ${suggested_rate:.0f}."
        )

        return {
            "estimated_cost": estimated_cost,
            "suggested_rate": suggested_rate,
            "recommendation": recommendation,
        }
