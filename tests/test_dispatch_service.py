from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "premafirm_ai_engine" / "services" / "dispatch_service.py"
spec = spec_from_file_location("dispatch_service", MODULE_PATH)
dispatch_service = module_from_spec(spec)
spec.loader.exec_module(dispatch_service)
DispatchService = dispatch_service.DispatchService


class _StopList(list):
    def sorted(self, attr):
        return _StopList(sorted(self, key=lambda item: getattr(item, attr)))

    def mapped(self, attr):
        return [getattr(item, attr) for item in self]


def test_compute_lead_totals_basic():
    vehicle = SimpleNamespace(
        home_location="5585 McAdam Rd, Mississauga, ON",
        service_type="dry",
        load_type="FTL",
    )
    stops = _StopList(
        [
            SimpleNamespace(
                sequence=1,
                address="Toronto, ON",
                pallets=10,
                weight_lbs=12000,
                service_type=False,
                load_type=False,
                stop_type="pickup",
            ),
            SimpleNamespace(
                sequence=2,
                address="Ottawa, ON",
                pallets=5,
                weight_lbs=5000,
                service_type=False,
                load_type=False,
                stop_type="delivery",
            ),
        ]
    )

    lead = SimpleNamespace(dispatch_stop_ids=stops, assigned_vehicle_id=vehicle)
    service = DispatchService(env=None)

    result = service.compute_lead_totals(lead)

    assert result["distance_km"] == 250.0
    assert result["aggregated_pallet_count"] == 15
    assert result["aggregated_load_weight_lbs"] == 17000
    assert result["service_type"] == "dry"
    assert result["load_type"] == "FTL"
    assert result["suggested_rate"] > result["estimated_cost"]


def test_compute_lead_totals_applies_dispatcher_rules_for_overweight_load():
    vehicle = SimpleNamespace(
        home_location="5585 McAdam Rd, Mississauga, ON",
        service_type="dry",
        load_type="FTL",
        payload_limit_lbs=13000,
        max_pallets=12,
    )
    stops = _StopList(
        [
            SimpleNamespace(
                sequence=1,
                address="Toronto, ON",
                pallets=8,
                weight_lbs=10000,
                service_type=False,
                load_type=False,
                stop_type="pickup",
                country="CA",
            ),
            SimpleNamespace(
                sequence=2,
                address="Montreal, QC",
                pallets=8,
                weight_lbs=7000,
                service_type=False,
                load_type=False,
                stop_type="delivery",
                country="CA",
            ),
        ]
    )

    lead = SimpleNamespace(
        dispatch_stop_ids=stops,
        assigned_vehicle_id=vehicle,
        total_weight_lbs=17000,
        total_pallets=16,
        deadhead_km=20,
        billing_mode="per_km",
    )
    service = DispatchService(env=None)

    result = service.compute_lead_totals(lead)

    assert result["decision"] == "REJECT_OVER_PAYLOAD"
    assert result["deadhead_km"] == 20.0
    assert result["total_km"] == 270.0
