import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _install_base_fakes():
    req = ModuleType("requests")
    req.get = lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
    sys.modules["requests"] = req


def _load_module(name, rel_path):
    _install_base_fakes()
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_mapbox_cache_model_has_required_fields_from_section5():
    source = (ROOT / "premafirm_ai_engine/models/mapbox_cache.py").read_text()
    for field_name in [
        "origin",
        "destination",
        "waypoint_hash",
        "departure_hour",
        "distance_km",
        "duration_minutes",
        "polyline",
        "cached_at",
    ]:
        assert field_name in source


def test_mapbox_cache_model_does_not_use_deprecated_cache_fields():
    source = (ROOT / "premafirm_ai_engine/models/mapbox_cache.py").read_text()
    assert "waypoints_hash" not in source
    assert "departure_date" not in source


def test_run_planner_uses_home_location_for_route_origin():
    source = (ROOT / "premafirm_ai_engine/services/run_planner_service.py").read_text()
    assert "home = run.vehicle_id.home_location" in source
    assert "calculate_trip_segments(home, ordered" in source


def test_validate_model_mapping_script_runs_cleanly():
    import subprocess

    result = subprocess.run([sys.executable, str(ROOT / "tools/validate_model_mapping.py")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout.lower()


def test_module_mapping_contains_required_ai_models():
    payload = json.loads((ROOT / "tools/module_model_map.json").read_text())
    aliases = {entry["alias"] for entry in payload["mappings"]}
    assert {"dispatch_plan", "dispatch_stop", "route_plan", "mapbox_cache"}.issubset(aliases)


def test_mapbox_service_uses_memory_and_db_cache_lookups():
    mod = _load_module("mapbox_service_section5", "premafirm_ai_engine/services/mapbox_service.py")
    source = (ROOT / "premafirm_ai_engine/services/mapbox_service.py").read_text()
    assert "self._memory_cache" in source
    assert "premafirm.mapbox.cache" in source
    assert "_cache_lookup" in source and "_cache_store" in source
    assert hasattr(mod.MapboxService, "_haversine_km")
