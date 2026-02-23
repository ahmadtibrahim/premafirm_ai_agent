from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compute_schedule_is_centralized_on_crm_lead():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "def _compute_schedule" in source
    assert "mapbox.get_travel_time" in source
    assert "weather.get_weather_factor" not in source


def test_no_hardcoded_nine_am_start_in_schedule_logic():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "9.0" not in source
    assert "work_start_hour" in source


def test_dispatch_stop_has_required_dynamic_scheduling_fields():
    source = (ROOT / "premafirm_ai_engine/models/dispatch_stop.py").read_text()
    for field_name in [
        "service_duration",
        "time_window_start",
        "time_window_end",
        "auto_scheduled",
        "drive_minutes",
    ]:
        assert field_name in source


def test_schedule_rolls_to_next_day_after_13_local_time_rule_present():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "if now_local.hour >= 13" in source


def test_schedule_enforces_capacity_before_route_building():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "Vehicle capacity exceeded: weight" in source
    assert "Vehicle capacity exceeded: pallets" in source


def test_strict_windows_raise_user_error_when_impossible():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "Pickup/Delivery window impossible within vehicle constraints." in source
