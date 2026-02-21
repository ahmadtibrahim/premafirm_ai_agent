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

    class _FakeFieldFactory:
        def __call__(self, *a, **k):
            return None

    class _FakeDatetime(_FakeFieldFactory):
        @staticmethod
        def now():
            return __import__("datetime").datetime(2026, 2, 18, 9, 0, 0)

    class _FakeDate(_FakeFieldFactory):
        @staticmethod
        def to_date(d):
            return d

    class _FakeFields(SimpleNamespace):
        def __getattr__(self, _name):
            return _FakeFieldFactory()

    odoo = ModuleType("odoo")
    odoo.fields = _FakeFields(Datetime=_FakeDatetime(), Date=_FakeDate())
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
        "features": [
            {
                "place_name": "Barrie, Ontario L4N 8Y1, Canada",
                "center": [-79.6, 44.3],
                "text": "Barrie",
                "place_type": ["place"],
                "context": [
                    {"id": "region.123", "short_code": "ca-on", "text": "Ontario"},
                    {"id": "postcode.1", "text": "L4N 8Y1"},
                    {"id": "country.1", "short_code": "ca", "text": "Canada"},
                ],
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



def test_classification_engine_marks_multistop_as_ltl():
    mod = _load_module("crm_lead_extension_test", "premafirm_ai_engine/models/crm_lead_extension.py")
    lead = mod.CrmLead()
    result = lead.classify_load(
        extracted_data={
            "pickup_locations_count": 2,
            "delivery_locations_count": 1,
            "additional_stops_planned": True,
            "exclusive_language_detected": False,
        }
    )
    assert result["classification"] == "LTL"
    assert result["confidence"] == "HIGH"


def test_ai_override_command_updates_mode_equipment_and_rate():
    mod = _load_module("crm_lead_extension_override_test", "premafirm_ai_engine/models/crm_lead_extension.py")

    class _Country:
        def __init__(self, cid):
            self.id = cid

    class _Env(dict):
        def ref(self, xmlid, raise_if_not_found=False):
            if xmlid == "base.ca":
                return _Country(2)
            if xmlid == "base.us":
                return _Country(1)
            return None

    lead = __import__("types").SimpleNamespace(
        ai_locked=False,
        ai_override_command="make it flat rate 800 reefer canada",
        billing_mode="per_km",
        equipment_type="dry",
        final_rate=100,
        partner_id=__import__("types").SimpleNamespace(country_id=None),
        env=_Env(),
        write=lambda vals: [setattr(lead, k, v) for k, v in vals.items()],
        compute_pricing=lambda: None,
        _create_ai_log=lambda user_modified=False: None,
    )

    mod.CrmLead.action_ai_override([lead])
    assert lead.billing_mode == "flat"
    assert lead.equipment_type == "reefer"
    assert lead.final_rate == 800.0


def test_dispatch_rules_json_includes_required_2026_sections():
    import json

    rules = json.loads((ROOT / "premafirm_ai_engine/data/dispatch_rules.json").read_text())
    for key in [
        "routing_engine",
        "email_intake_engine",
        "product_selection_engine",
        "time_window_scheduler",
        "hos_engine",
        "weather_integration",
        "booking_decision_output",
    ]:
        assert key in rules


def test_dispatch_rules_engine_uses_product_and_accessorial_mapping_from_json():
    mod = _load_module("dispatch_rules_engine_test", "premafirm_ai_engine/services/dispatch_rules_engine.py")
    rules = mod.DispatchRulesEngine(env=None)
    assert rules.select_product("United States", "FTL", "dry") == 266
    assert rules.select_product("Canada", "LTL", "reefer") == 273
    assert rules.accessorial_product_ids(liftgate=True, inside_delivery=True) == [269, 270]


def test_crm_dispatch_service_instantiates_rules_engine_for_accessorial_selection():
    source = (ROOT / "premafirm_ai_engine/services/crm_dispatch_service.py").read_text()
    assert "DispatchRulesEngine(self.env).accessorial_product_ids" in source


def test_load_section_parser_supports_multiple_load_marker_formats():
    mod = _load_module("ai_extraction_load_markers_test", "premafirm_ai_engine/services/ai_extraction_service.py")
    svc = mod.AIExtractionService(env=None)
    raw_text = """
Load No. 1
Pickup Address: A St, Toronto, ON
Delivery Address: B St, Ottawa, ON
Pallets: 4
Total Weight: 4500 lbs

LOAD 2
Pickup Address: C St, Barrie, ON
Delivery Address: D St, Mississauga, ON
Pallets: 5
Total Weight: 5100 lbs
"""
    parsed = svc._parse_load_sections(raw_text)
    assert len(parsed["stops"]) == 4
    assert parsed["stops"][0]["load_name"] == "LOAD #1"
    assert parsed["stops"][2]["load_name"] == "LOAD #2"


def test_load_section_parser_treats_attachment_without_load_markers_as_single_load():
    mod = _load_module("ai_extraction_single_load_test", "premafirm_ai_engine/services/ai_extraction_service.py")
    svc = mod.AIExtractionService(env=None)
    raw_text = """
Purchase Order
Pickup Address: 55 Commerce Park Dr, Barrie, ON
Delivery Address: 6350 Tomken Rd, Mississauga, ON
Pallets: 8
Total Weight: 9115 lbs
"""
    parsed = svc._parse_load_sections(raw_text)
    assert len(parsed["stops"]) == 2
    assert parsed["stops"][0]["load_name"] is None


def test_crm_load_info_grid_keeps_single_load_column_editable():
    view_text = (ROOT / "premafirm_ai_engine/views/crm_view.xml").read_text()
    assert 'name="load_id" string="Load #"' in view_text
    assert 'name="load_number"' not in view_text
