from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_pricing_engine():
    module_path = Path(__file__).resolve().parent / "pricing_engine.py"
    spec = spec_from_file_location("pricing_engine", module_path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PricingEngine


PricingEngine = _load_pricing_engine()


class DispatchService:
    """Compatibility service for unit tests and lightweight computations."""

    def __init__(self, env):
        self.env = env
        self.pricing_engine = PricingEngine(env)

    def _estimate_distance_km(self, lead):
        return 250.0 if len(lead.dispatch_stop_ids) >= 2 else 0.0

    def compute_lead_totals(self, lead):
        distance_km = self._estimate_distance_km(lead)
        aggregated_pallet_count = sum(float(stop.pallets or 0) for stop in lead.dispatch_stop_ids)
        aggregated_load_weight_lbs = sum(float(stop.weight_lbs or 0) for stop in lead.dispatch_stop_ids)

        service_type = lead.assigned_vehicle_id.service_type if lead.assigned_vehicle_id else "dry"
        load_type = lead.assigned_vehicle_id.load_type if lead.assigned_vehicle_id else "FTL"

        dispatch_stops = []
        for stop in lead.dispatch_stop_ids:
            dispatch_stops.append(
                type(
                    "Stop",
                    (),
                    {
                        "stop_type": getattr(stop, "stop_type", "delivery" if stop.sequence > 1 else "pickup"),
                        "service_type": stop.service_type or service_type,
                    },
                )
            )

        pricing_lead = type(
            "PricingLead",
            (),
            {
                "dispatch_stop_ids": dispatch_stops,
                "total_distance_km": distance_km,
                "total_drive_hours": 4.0,
                "total_pallets": aggregated_pallet_count,
                "total_weight_lbs": aggregated_load_weight_lbs,
                "deadhead_km": float(getattr(lead, "deadhead_km", 0.0) or 0.0),
                "zone": getattr(lead, "zone", False),
                "unpaid_deadhead": bool(getattr(lead, "unpaid_deadhead", False)),
                "assigned_vehicle_id": lead.assigned_vehicle_id,
                "liftgate": False,
                "inside_delivery": False,
                "detention_requested": False,
            },
        )
        pricing = self.pricing_engine.calculate_pricing(pricing_lead)

        result = {
            "distance_km": distance_km,
            "aggregated_pallet_count": aggregated_pallet_count,
            "aggregated_load_weight_lbs": aggregated_load_weight_lbs,
            "service_type": service_type,
            "load_type": load_type,
            "estimated_cost": pricing["estimated_cost"],
            "suggested_rate": pricing["suggested_rate"],
        }
        for key in (
            "start_location",
            "deadhead_km",
            "total_km",
            "gross_revenue",
            "base_cost",
            "overnight_cost",
            "detention_cost",
            "NET_profit",
            "score",
            "decision",
        ):
            if key in pricing:
                result[key] = pricing[key]
        return result
