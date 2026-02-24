import ast
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PERCENT_FIELD = "legacy_adj_" + "percent"
AMOUNT_FIELD = "legacy_adj_" + "amount"
BILLING_MODE_FIELD = "billing_" + "mode"


def test_crm_lead_legacy_adj_fields_removed_from_model_and_form_view():
    model_source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()
    view_source = (ROOT / "premafirm_ai_engine/views/crm_view.xml").read_text()

    assert PERCENT_FIELD not in model_source
    assert AMOUNT_FIELD not in model_source
    assert f'name="{PERCENT_FIELD}"' not in view_source
    assert f'name="{AMOUNT_FIELD}"' not in view_source


def test_crm_form_view_xml_is_well_formed_after_legacy_adj_field_removal_regression():
    view_path = ROOT / "premafirm_ai_engine/views/crm_view.xml"
    parsed = ET.parse(view_path)
    root = parsed.getroot()
    assert root.tag == "odoo"


def test_no_duplicate_method_definitions_in_mapbox_service():
    source_path = ROOT / "premafirm_ai_engine/services/mapbox_service.py"
    module = ast.parse(source_path.read_text())
    class_defs = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "MapboxService"]
    assert class_defs, "MapboxService class missing"
    methods = [node.name for node in class_defs[0].body if isinstance(node, ast.FunctionDef)]
    duplicates = [name for name, count in Counter(methods).items() if count > 1]
    assert not duplicates, f"Duplicate methods found: {duplicates}"


def test_no_legacy_adj_field_references_remain_in_module_python_or_xml_sources():
    base = ROOT / "premafirm_ai_engine"
    for path in list(base.rglob("*.py")) + list(base.rglob("*.xml")):
        source = path.read_text()
        assert PERCENT_FIELD not in source, f"{PERCENT_FIELD} found in {path}"
        assert AMOUNT_FIELD not in source, f"{AMOUNT_FIELD} found in {path}"


def test_no_billing_mode_references_remain_in_module_python_or_xml_sources():
    base = ROOT / "premafirm_ai_engine"
    allowed_cleanup_file = base / "models/crm_lead_extension.py"
    for path in list(base.rglob("*.py")) + list(base.rglob("*.xml")):
        source = path.read_text()
        if path == allowed_cleanup_file:
            continue
        assert BILLING_MODE_FIELD not in source, f"{BILLING_MODE_FIELD} found in {path}"


def test_crm_lead_register_hook_cleans_stale_manual_billing_mode_field():
    model_source = (ROOT / "premafirm_ai_engine/models/crm_lead_extension.py").read_text()

    assert "def _register_hook" in model_source
    assert "ir.model.fields" in model_source
    assert '("name", "=", "billing_mode")' in model_source
    assert '("state", "=", "manual")' in model_source
