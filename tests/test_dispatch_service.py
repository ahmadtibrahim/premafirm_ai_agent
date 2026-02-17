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
        x_studio_location="5585 McAdam Rd, Mississauga, ON",
        x_studio_service_type="dry",
        x_studio_load_type="FTL",
    )
    stops = _StopList(
        [
            SimpleNamespace(
                sequence=1,
                address="Toronto, ON",
                stop_pickup_dt=False,
                stop_delivery_dt=False,
                pallet_count=10,
                load_weight_lbs=12000,
                service_type=False,
                load_type=False,
            ),
            SimpleNamespace(
                sequence=2,
                address="Ottawa, ON",
                stop_pickup_dt=False,
                stop_delivery_dt=False,
                pallet_count=5,
                load_weight_lbs=5000,
                service_type=False,
                load_type=False,
            ),
        ]
    )

    lead = SimpleNamespace(stop_ids=stops, x_studio_assigned_vehicle=vehicle)
    service = DispatchService(env=None)

    result = service.compute_lead_totals(lead)

    assert result["distance_km"] == 250.0
    assert result["aggregated_pallet_count"] == 15
    assert result["aggregated_load_weight_lbs"] == 17000
    assert result["service_type"] == "dry"
    assert result["load_type"] == "FTL"
    assert result["suggested_rate"] > result["estimated_cost"]
