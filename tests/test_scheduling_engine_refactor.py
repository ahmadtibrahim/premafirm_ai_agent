from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compute_schedule_is_centralized_on_crm_lead():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "def _compute_schedule" in source
    assert "mapbox.get_travel_time" in source
    assert "weather.get_weather_factor" in source


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
