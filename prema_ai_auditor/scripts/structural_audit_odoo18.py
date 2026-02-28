#!/usr/bin/env python3
"""Structural audit for Odoo 18 module against /reference snapshots."""

from __future__ import annotations

import ast
import csv
import re
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference"
DOC = ROOT / "docs" / "STRUCTURAL_AUDIT_REPORT.md"

MODEL_LINE_RE = re.compile(r"\|\s*([a-z0-9_]+\.[a-z0-9_.]+)\s*\|")
FIELD_TUPLE_RE = re.compile(r"\((?:'|\")([a-zA-Z0-9_]+)(?:'|\")\s*,")
GROUP_REF_RE = re.compile(r"groups\s*=\s*['\"]([^'\"]+)['\"]")
REF_CALL_RE = re.compile(r"ref\(['\"]([^'\"]+)['\"]\)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_reference_models() -> set[str]:
    out = set()
    for line in read(REF / "01_all_models.sql").splitlines():
        m = MODEL_LINE_RE.search(line)
        if m:
            out.add(m.group(1))
    return out


def parse_reference_fields() -> dict[str, dict[str, dict[str, str]]]:
    fields: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for line in read(REF / "02_all_fields.sql").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        model, field, _label, ftype, _rel = parts[:5]
        if "." not in model or not field:
            continue
        fields[model][field] = {"type": ftype, "relation": _rel}
    return dict(fields)


def parse_local_models_and_fields() -> tuple[set[str], dict[str, set[str]], dict[str, dict[str, str]]]:
    local_models = set()
    local_fields: dict[str, set[str]] = defaultdict(set)
    local_relations: dict[str, dict[str, str]] = defaultdict(dict)
    for py in (ROOT / "models").rglob("*.py"):
        src = read(py)
        model_names = re.findall(r"_name\s*=\s*['\"]([^'\"]+)['\"]", src)
        field_names = re.findall(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*fields\.", src, re.M)
        if not model_names:
            continue
        for model in model_names:
            local_models.add(model)
            for fname in field_names:
                local_fields[model].add(fname)
            for relm in re.finditer(
                r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*fields\.(Many2one|One2many|Many2many)\((?:'|\")([^'\"]+)(?:'|\")",
                src,
                re.M,
            ):
                local_relations[model][relm.group(1)] = relm.group(3)
    return local_models, dict(local_fields), dict(local_relations)


def parse_actions_and_menus() -> tuple[dict[str, dict], list[dict], set[str], list[tuple[str, str]]]:
    actions: dict[str, dict] = {}
    menus: list[dict] = []
    xml_ids = set()
    group_refs: list[tuple[str, str]] = []

    for xml_file in (ROOT / "views").glob("*.xml"):
        root = ET.fromstring(read(xml_file))
        for rec in root.findall("record"):
            xml_id = rec.get("id")
            if xml_id:
                xml_ids.add(xml_id)
            model = rec.get("model")
            if model == "ir.actions.act_window":
                action = {"file": str(xml_file.relative_to(ROOT)), "id": xml_id, "res_model": None, "view_mode": None}
                for field in rec.findall("field"):
                    if field.get("name") == "res_model":
                        action["res_model"] = (field.text or "").strip()
                    if field.get("name") == "view_mode":
                        action["view_mode"] = (field.text or "").strip()
                actions[xml_id] = action
        for menu in root.findall("menuitem"):
            m = {
                "file": str(xml_file.relative_to(ROOT)),
                "id": menu.get("id"),
                "action": menu.get("action"),
                "groups": menu.get("groups", ""),
            }
            menus.append(m)
            if m["id"]:
                xml_ids.add(m["id"])
            if m["groups"]:
                for grp in [g.strip() for g in m["groups"].split(",") if g.strip()]:
                    group_refs.append((m["file"], grp))

    sec = ROOT / "security" / "security.xml"
    if sec.exists():
        sroot = ET.fromstring(read(sec))
        for rec in sroot.findall("record"):
            rid = rec.get("id")
            if rid:
                xml_ids.add(f"prema_ai_auditor.{rid}")
                xml_ids.add(rid)
        stext = read(sec)
        for ref in REF_CALL_RE.findall(stext):
            if ref.startswith("prema_ai_auditor."):
                group_refs.append(("security/security.xml", ref))
    return actions, menus, xml_ids, group_refs


def parse_manifest_order() -> list[str]:
    manifest = ast.literal_eval(read(ROOT / "__manifest__.py"))
    return manifest.get("data", [])


def _extract_domain_tuple_fields(expr: str) -> list[str]:
    out = []
    try:
        val = ast.literal_eval(expr)
    except Exception:
        return out
    stack = [val]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (list, tuple)):
            if len(cur) >= 2 and isinstance(cur[0], str):
                out.append(cur[0])
            for item in cur:
                stack.append(item)
    return out


def parse_field_usages(rel_lookup: dict[str, dict[str, str]]) -> list[dict]:
    usages: list[dict] = []

    def walk(node, model: str, rel: str, source: str):
        for child in list(node):
            if child.tag == "field" and child.get("name"):
                fname = child.get("name")
                usages.append({"file": rel, "source": source, "model": model, "field": fname})
                next_model = rel_lookup.get(model, {}).get(fname, model)
                walk(child, next_model, rel, source)
            else:
                walk(child, model, rel, source)

    for xml_file in (ROOT / "views").glob("*.xml"):
        rel = str(xml_file.relative_to(ROOT))
        root = ET.fromstring(read(xml_file))
        for rec in root.findall("record"):
            if rec.get("model") != "ir.ui.view":
                continue
            model = None
            for field in rec.findall("field"):
                if field.get("name") == "model":
                    model = (field.text or "").strip()
            arch = rec.find("field[@name='arch']")
            if arch is None or model is None:
                continue
            walk(arch, model, rel, "view")
            for elem in arch.iter():
                for attr in ("domain", "context", "attrs"):
                    val = elem.get(attr)
                    if not val:
                        continue
                    for fname in _extract_domain_tuple_fields(val):
                        usages.append({"file": rel, "source": attr, "model": model, "field": fname})
    return usages


def main() -> None:
    ref_models = parse_reference_models()
    ref_fields = parse_reference_fields()
    local_models, local_fields, local_relations = parse_local_models_and_fields()
    actions, menus, xml_ids, group_refs = parse_actions_and_menus()
    manifest_data = parse_manifest_order()
    rel_lookup: dict[str, dict[str, str]] = defaultdict(dict)
    for m, fmeta in ref_fields.items():
        for fname, info in fmeta.items():
            if info.get("relation"):
                rel_lookup[m][fname] = info["relation"]
    for m, rels in local_relations.items():
        rel_lookup[m].update(rels)

    field_usages = parse_field_usages(dict(rel_lookup))

    model_universe = ref_models | local_models

    missing_action_models = [a for a in actions.values() if a["res_model"] not in model_universe]
    missing_menu_actions = [m for m in menus if m.get("action") and m["action"] not in xml_ids]
    menu_action_model_missing = []
    for m in menus:
        aid = m.get("action")
        if aid in actions and actions[aid]["res_model"] not in model_universe:
            menu_action_model_missing.append((m, actions[aid]))

    known_groups = set()
    for line in read(REF / "02_all_fields.sql").splitlines():
        if "| res.groups |" in line:
            p = [x.strip() for x in line.split("|")]
            if len(p) > 1:
                known_groups.add("res.groups")
    known_groups.update({"prema_ai_auditor.group_prema_ai_auditor", "prema_ai_auditor.group_prema_ai_master"})
    missing_groups = sorted({g for _, g in group_refs if g and g not in known_groups and "." in g})

    missing_fields = []
    for u in field_usages:
        model = u["model"]
        field = u["field"]
        if field in {"id", "display_name", "create_date", "write_date", "__last_update"}:
            continue
        in_ref = field in ref_fields.get(model, {})
        in_local = field in local_fields.get(model, set())
        if not in_ref and not in_local:
            missing_fields.append(u)

    access_model_ids = set()
    with (ROOT / "security" / "ir.model.access.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            access_model_ids.add(row["model_id:id"].strip())
    model_defs_without_access = sorted(
        m
        for m in local_models
        if f"model_{m.replace('.', '_')}" not in access_model_ids and not m.endswith("wizard")
    )

    # strict ordering check
    security_idx = [i for i, p in enumerate(manifest_data) if p.startswith("security/")]
    menu_idx = [i for i, p in enumerate(manifest_data) if p.startswith("views/menu")]
    data_idx = [i for i, p in enumerate(manifest_data) if p.startswith("data/") or p.endswith("_action.xml")]
    views_idx = [i for i, p in enumerate(manifest_data) if p.startswith("views/") and not p.startswith("views/menu") and not p.endswith("_action.xml")]
    wrong_order = False
    if security_idx and data_idx and min(data_idx) < max(security_idx):
        wrong_order = True
    if data_idx and views_idx and min(views_idx) < max(data_idx):
        wrong_order = True
    if menu_idx and views_idx and min(menu_idx) < max(views_idx):
        wrong_order = True

    lines = [
        "# STRUCTURAL AUDIT REPORT",
        "",
        "## Missing models (action res_model not found)",
    ]
    if missing_action_models:
        for a in missing_action_models:
            lines.append(f"- {a['id']} ({a['file']}): `{a['res_model']}`")
    else:
        lines.append("- None")

    lines += ["", "## Missing fields", "| file | model | field | source |", "|---|---|---|---|"]
    if missing_fields:
        for u in missing_fields:
            lines.append(f"| {u['file']} | {u['model']} | {u['field']} | {u['source']} |")
    else:
        lines.append("| - | - | - | None |")

    lines += ["", "## Missing actions",]
    if missing_menu_actions:
        for m in missing_menu_actions:
            lines.append(f"- {m['file']}::{m['id']} references missing action `{m['action']}`")
    else:
        lines.append("- None")

    lines += ["", "## Invalid references",]
    if menu_action_model_missing:
        for menu, action in menu_action_model_missing:
            lines.append(f"- Menu `{menu['id']}` -> action `{action['id']}` invalid model `{action['res_model']}`")
    else:
        lines.append("- None")

    lines += ["", "## Wrong load order",]
    if wrong_order:
        lines.append("- `__manifest__.py` data list is not in strict sequence: security -> data/actions -> views -> menus")
    else:
        lines.append("- None")

    lines += ["", "## Security gaps",]
    if missing_groups:
        for g in missing_groups:
            lines.append(f"- Unknown group reference: `{g}`")
    else:
        lines.append("- None")
    if model_defs_without_access:
        lines.append("- Models without explicit access rule:")
        for m in model_defs_without_access:
            lines.append(f"  - `{m}`")

    lines += [
        "",
        "## Odoo 18 list view migration",
        "- Any remaining `<tree>` architecture tags and `view_mode=tree` must be migrated to `<list>` and `view_mode=list`.",
        "",
        "## Proposed fixes",
        "1. Replace legacy tree declarations with list views in XML and act_window view_mode.",
        "2. Reorder manifest data load to strict production-safe sequence.",
        "3. Add missing access rights entries for local models where required.",
    ]

    DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {DOC}")


if __name__ == "__main__":
    main()
