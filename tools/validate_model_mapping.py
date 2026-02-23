#!/usr/bin/env python3
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MAP_FILE = ROOT / "tools" / "module_model_map.json"
SCHEMA_FILE = ROOT / "tools" / "odoo_full_schema.json"


def main():
    mapping = json.loads(MAP_FILE.read_text())
    schema = json.loads(SCHEMA_FILE.read_text()).get("models", {})
    errors = []

    seen_alias = set()
    for entry in mapping.get("mappings", []):
        model = entry.get("model")
        alias = entry.get("alias")
        fields = entry.get("fields", [])

        if alias in seen_alias:
            errors.append(f"duplicate mapping alias: {alias}")
        seen_alias.add(alias)

        if model not in schema:
            errors.append(f"model missing in schema: {model}")
            continue

        model_fields = set(schema.get(model, []))
        for field in fields:
            if field not in model_fields:
                errors.append(f"missing field '{field}' on model '{model}'")

    mapped_models = {entry.get("model") for entry in mapping.get("mappings", [])}
    orphan_models = sorted(set(schema.keys()) - mapped_models)
    if orphan_models:
        errors.append(f"orphan schema models not mapped: {', '.join(orphan_models)}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        return 1

    print("Model mapping validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
