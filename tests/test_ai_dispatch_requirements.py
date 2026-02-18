import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _install_base_fakes():
    req = ModuleType("requests")
    req.get = lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
    req.post = lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "{}"}}]})
    sys.modules["requests"] = req

    odoo = ModuleType("odoo")
    odoo.fields = SimpleNamespace(
        Datetime=SimpleNamespace(now=lambda: __import__("datetime").datetime(2026, 2, 18, 9, 0, 0)),
        Date=SimpleNamespace(to_date=lambda d: d),
    )
    odoo.models = SimpleNamespace(Model=object)
    odoo.api = SimpleNamespace(depends=lambda *a, **k: (lambda f: f), onchange=lambda *a, **k: (lambda f: f), constrains=lambda *a, **k: (lambda f: f))
    sys.modules["odoo"] = odoo
    ex = ModuleType("odoo.exceptions")
    ex.UserError = Exception
    ex.ValidationError = Exception
    sys.modules["odoo.exceptions"] = ex
    tools = ModuleType("odoo.tools")
    tools.html2plaintext = lambda v: v
    sys.modules["odoo.tools"] = tools


def _load_module(name, rel_path):
    _install_base_fakes()
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_attachment_parser_supports_pickup_delivery_address_labels():
    mod = _load_module("ai_extraction_service_test", "premafirm_ai_engine/services/ai_extraction_service.py")
    svc = mod.AIExtractionService(env=None)
    raw_text = """
LOAD #1
Pickup Address
780 Third Ave, Brooklyn, NY 11232, USA
Delivery Address
500 Main St, Newark, NJ 07102, USA
# of Pallets
10
Total Weight
12000 lbs

LOAD #2
Pickup Address: Barrie, ON L4N 8Y1, Canada
Delivery Address: Mississauga, ON L5T 2N1, Canada
Pallets: 8
Weight: 9800 lbs
"""
    parsed = svc._parse_load_sections(raw_text)
    assert len(parsed["stops"]) == 4


def test_message_selection_prefers_email_with_attachment():
    pkg = ModuleType("premafirm_ai_engine")
    pkg.__path__ = []
    sys.modules["premafirm_ai_engine"] = pkg
    models_pkg = ModuleType("premafirm_ai_engine.models")
    models_pkg.__path__ = []
    sys.modules["premafirm_ai_engine.models"] = models_pkg
    services_pkg = ModuleType("premafirm_ai_engine.services")
    services_pkg.__path__ = []
    sys.modules["premafirm_ai_engine.services"] = services_pkg
    crm_dispatch_stub = ModuleType("premafirm_ai_engine.services.crm_dispatch_service")
    crm_dispatch_stub.CRMDispatchService = object
    sys.modules["premafirm_ai_engine.services.crm_dispatch_service"] = crm_dispatch_stub

    mod = _load_module("premafirm_ai_engine.models.ai_engine", "premafirm_ai_engine/models/ai_engine.py")

    class Msg:
        def __init__(self, date, mtype, body="", attachments=None):
            self.model = "crm.lead"
            self.res_id = 7
            self.date = date
            self.message_type = mtype
            self.body = body
            self.attachment_ids = attachments or []

    class MsgList(list):
        def sorted(self, field, reverse=False):
            return MsgList(sorted(self, key=lambda m: m.date, reverse=reverse))

    lead = SimpleNamespace(
        id=7,
        message_ids=MsgList([Msg(3, "comment", body="latest"), Msg(2, "email", body="LOAD #", attachments=[1])]),
        env={"mail.message": object()},
    )
    selected = mod.CrmLeadAI._get_latest_email_message(lead)
    assert selected.message_type == "email"


def test_city_extraction_prefers_city_region_not_region_only():
    mod = _load_module("mapbox_service_test", "premafirm_ai_engine/services/mapbox_service.py")

    class FakeConfig:
        def sudo(self):
            return self

        def get_param(self, _):
            return "k"

    svc = mod.MapboxService({"ir.config_parameter": FakeConfig()})
    svc._safe_get = lambda _url, timeout=20: {
        "results": [
            {
                "formatted_address": "Barrie, ON L4N 8Y1, Canada",
                "geometry": {"location": {"lat": 44.3, "lng": -79.6}},
                "address_components": [
                    {"long_name": "Barrie", "types": ["administrative_area_level_2"]},
                    {"short_name": "ON", "types": ["administrative_area_level_1"]},
                    {"long_name": "L4N 8Y1", "types": ["postal_code"]},
                ],
                "types": ["street_address"],
            }
        ]
    }
    geo = svc.geocode_address("Barrie, ON")
    assert geo["short_address"] == "Barrie, ON"


def test_run_insertion_reduces_empty_km():
    map_mod = _load_module("premafirm_ai_engine.services.mapbox_service", "premafirm_ai_engine/services/mapbox_service.py")
    sys.modules["premafirm_ai_engine.services.mapbox_service"] = map_mod
    run_mod = _load_module("premafirm_ai_engine.services.run_planner_service", "premafirm_ai_engine/services/run_planner_service.py")

    class Stop(SimpleNamespace):
        pass

    run = SimpleNamespace(vehicle_id=SimpleNamespace(home_location="Home"), stop_ids=[])
    planner = run_mod.RunPlannerService(env=None)
    matrix = {
        ("Home", "Barrie PU"): (100, 1.2),
        ("Barrie PU", "Mississauga DEL"): (90, 1.0),
        ("Home", "Toronto PU"): (25, 0.4),
        ("Toronto PU", "Barrie DEL"): (85, 1.1),
        ("Barrie DEL", "Barrie PU"): (5, 0.1),
    }

    def fake_segments(stops, origin_address=None):
        prev = origin_address
        out = []
        for idx, stop in enumerate(stops, 1):
            km, hr = matrix.get((prev, stop.address), (50, 0.8))
            out.append({"sequence": idx, "distance_km": km, "drive_hours": hr})
            prev = stop.address
        return out

    planner.map_service.calculate_trip_segments = fake_segments
    base = planner.simulate_run(run, [Stop(address="Barrie PU", stop_service_mins=60, cargo_delta=1), Stop(address="Mississauga DEL", stop_service_mins=45, cargo_delta=-1)])
    best = planner.simulate_run(run, [Stop(address="Toronto PU", stop_service_mins=60, cargo_delta=1), Stop(address="Barrie DEL", stop_service_mins=45, cargo_delta=-1), Stop(address="Barrie PU", stop_service_mins=60, cargo_delta=1), Stop(address="Mississauga DEL", stop_service_mins=45, cargo_delta=-1)])
    assert best["empty_distance_km"] < base["empty_distance_km"]
