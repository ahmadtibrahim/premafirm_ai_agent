#!/usr/bin/env python3
"""Validate code-used models/fields against /reference snapshots (read-only)."""

from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REF = ROOT / "reference"

MODEL_RE = re.compile(r"\|\s*([a-z0-9_]+\.[a-z0-9_.]+)\s*\|")


def parse_runtime_specs():
    models = set()
    fields = {}
    spec_path = REF / "07_python_model_specs.txt"
    for line in spec_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        # expected pattern: model.field (type)
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([a-z0-9_]+\.[a-z0-9_.]+)\.([a-zA-Z0-9_]+)\s*[:\-]\s*([a-zA-Z0-9_]+)", line)
        if m:
            model, field, ftype = m.groups()
            models.add(model)
            fields.setdefault(model, {})[field] = ftype
    return models, fields


def parse_all_fields_sql():
    fields = {}
    for line in (REF / "02_all_fields.sql").read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        model, field, _label, ftype, _relation = parts[:5]
        if "." not in model or not field:
            continue
        fields.setdefault(model, {})[field] = ftype
    return fields


def parse_all_models_sql():
    models = set()
    for line in (REF / "01_all_models.sql").read_text(encoding="utf-8", errors="ignore").splitlines():
        m = MODEL_RE.search(line)
        if m:
            models.add(m.group(1))
    return models


def load_used_models():
    used_path = DOCS / "used_models.txt"
    return {line.strip() for line in used_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def load_used_fields():
    used = []
    with (DOCS / "used_fields.txt").open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            used.append(row)
    return used


def main():
    runtime_models, runtime_fields = parse_runtime_specs()
    sql_fields = parse_all_fields_sql()
    all_models = parse_all_models_sql()

    used_models = load_used_models()
    used_fields = load_used_fields()

    missing_models = sorted(m for m in used_models if m not in runtime_models and m not in all_models)

    missing_fields = []
    for row in used_fields:
        model = row["model"]
        field = row["field"]
        if model in {"unknown", "view"}:
            continue
        runtime_model_fields = runtime_fields.get(model)
        sql_model_fields = sql_fields.get(model, {})
        if runtime_model_fields is not None:
            exists = field in runtime_model_fields
        else:
            exists = field in sql_model_fields
        if not exists:
            missing_fields.append(row)

    report = [
        "# Mapping Drift Report",
        "",
        "## Missing Models (HARD FAIL)",
    ]
    if missing_models:
        report.extend(f"- `{model}`: guard feature or remove usage." for model in missing_models)
    else:
        report.append("- None")

    report.extend(["", "## Missing Fields", "| model | field | source | remediation |", "|---|---|---|---|"])
    if missing_fields:
        for row in missing_fields:
            report.append(
                f"| {row['model']} | {row['field']} | {row['source']} | Add mapping override (ICP) or disable feature via soft guard. |"
            )
    else:
        report.append("| - | - | - | No missing fields detected for model-scoped usage. |")

    report.extend(
        [
            "",
            "## Type Mismatches",
            "- Static type mismatches are marked advisory only in this run; enrich extractor with write-path type inference for strict blocking.",
            "",
            "## Studio/Custom-Only Field Risk",
            "- Fields found in SQL snapshot but not runtime specs should be treated as environment-specific and guarded with config mapping.",
        ]
    )

    (DOCS / "mapping_drift_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
