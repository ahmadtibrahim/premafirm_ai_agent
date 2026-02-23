import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_crm_lead_keeps_legacy_discount_fields_for_view_compatibility():
    source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    assert "discount_percent = fields.Float" in source
    assert "discount_amount = fields.Monetary" in source


def test_no_duplicate_method_definitions_in_mapbox_service():
    source_path = ROOT / "premafirm_ai_engine/services/mapbox_service.py"
    module = ast.parse(source_path.read_text())
    class_defs = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "MapboxService"]
    assert class_defs, "MapboxService class missing"
    methods = [node.name for node in class_defs[0].body if isinstance(node, ast.FunctionDef)]
    duplicates = [name for name, count in Counter(methods).items() if count > 1]
    assert not duplicates, f"Duplicate methods found: {duplicates}"
