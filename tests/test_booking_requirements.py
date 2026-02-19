import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def _fake_odoo():
    class _FieldFactory:
        def __call__(self, *a, **k):
            return None

    class _Datetime(_FieldFactory):
        @staticmethod
        def to_datetime(v):
            return v if isinstance(v, datetime) else datetime.fromisoformat(v)

    api = SimpleNamespace(
        depends=lambda *a, **k: (lambda f: f),
        onchange=lambda *a, **k: (lambda f: f),
        constrains=lambda *a, **k: (lambda f: f),
        model_create_multi=lambda f: f,
    )
    fields = SimpleNamespace(
        Many2one=_FieldFactory(),
        Datetime=_Datetime(),
        Float=_FieldFactory(),
        Selection=_FieldFactory(),
        Char=_FieldFactory(),
    )
    models = SimpleNamespace(Model=object)
    odoo = ModuleType("odoo")
    odoo.api = api
    odoo.fields = fields
    odoo.models = models
    sys.modules["odoo"] = odoo

    exc = ModuleType("odoo.exceptions")
    exc.ValidationError = Exception
    sys.modules["odoo.exceptions"] = exc


def _load_booking_module():
    _fake_odoo()
    spec = importlib.util.spec_from_file_location(
        "premafirm_booking_test", ROOT / "premafirm_ai_engine/models/premafirm_booking.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_booking_onchange_sets_six_hour_default_end_time():
    mod = _load_booking_module()
    booking = SimpleNamespace(
        lead_id=1,
        start_datetime=datetime(2026, 1, 1, 8, 0, 0),
        end_datetime=None,
    )
    mod.PremafirmBooking._onchange_start_datetime(booking)
    assert booking.end_datetime == datetime(2026, 1, 1, 14, 0, 0)


def test_duration_hours_computation_uses_datetime_delta():
    mod = _load_booking_module()
    rec = SimpleNamespace(
        start_datetime=datetime(2026, 1, 1, 8, 0, 0),
        end_datetime=datetime(2026, 1, 1, 17, 30, 0),
        duration_hours=0.0,
    )
    mod.PremafirmBooking._compute_duration_hours([rec])
    assert rec.duration_hours == 9.5
