from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_pricing_engine():
    module_path = Path(__file__).resolve().parent / 'pricing_engine.py'
    spec = spec_from_file_location('pricing_engine', module_path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PricingEngine


PricingEngine = _load_pricing_engine()


class DispatchService:
    def __init__(self, env):
        self.env = env
        self.pricing_engine = PricingEngine(env)

    def _estimate_distance_km(self, lead):
        if len(lead.dispatch_stop_ids) >= 2:
            return 250.0
        return 0.0

    def compute_lead_totals(self, lead):
        distance_km = self._estimate_distance_km(lead)
        aggregated_pallet_count = sum(stop.pallets for stop in lead.dispatch_stop_ids)
        aggregated_load_weight_lbs = sum(stop.weight_lbs for stop in lead.dispatch_stop_ids)

        service_type = lead.assigned_vehicle_id.service_type if lead.assigned_vehicle_id else "dry"
        load_type = lead.assigned_vehicle_id.load_type if lead.assigned_vehicle_id else "FTL"

        dispatch_stops = []
        for stop in lead.dispatch_stop_ids:
            dispatch_stops.append(type("Stop", (), {
                "stop_type": "delivery" if stop.sequence > 1 else "pickup",
                "service_type": stop.service_type or service_type,
            }))

        pricing_lead = type("PricingLead", (), {
            "dispatch_stop_ids": dispatch_stops,
            "total_distance_km": distance_km,
            "total_drive_hours": 4.0,
            "total_pallets": aggregated_pallet_count,
            "liftgate": False,
            "inside_delivery": False,
            "detention_requested": False,
        })
        pricing = self.pricing_engine.calculate_pricing(pricing_lead)

        return {
            "distance_km": distance_km,
            "aggregated_pallet_count": aggregated_pallet_count,
            "aggregated_load_weight_lbs": aggregated_load_weight_lbs,
            "service_type": service_type,
            "load_type": load_type,
            "estimated_cost": pricing["estimated_cost"],
            "suggested_rate": pricing["suggested_rate"],
        }
