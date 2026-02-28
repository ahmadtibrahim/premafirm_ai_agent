#!/usr/bin/env python3
"""Static extractor for model/field usage in prema_ai_auditor (read-only vs /reference)."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs"
OUT_DIR.mkdir(exist_ok=True)

CODE_GLOBS = ("*.py", "*.xml", "*.js")
SKIP_PARTS = {"reference", ".git", "__pycache__"}

ENV_MODEL_RE = re.compile(r"(?:self\.)?env\[['\"]([^'\"]+)['\"]\]")
M2O_RE = re.compile(r"fields\.(?:Many2one|One2many|Many2many)\(\s*['\"]([^'\"]+)['\"]")
XML_FIELD_RE = re.compile(r"<field[^>]*\sname=['\"]([^'\"]+)['\"]")
JS_FIELD_RE = re.compile(r"['\"]([a-z_][a-z0-9_]{1,})['\"]")


def iter_files():
    for pattern in CODE_GLOBS:
        for path in ROOT.rglob(pattern):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            yield path


def parse_py(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    models = set(m.group(1) for m in ENV_MODEL_RE.finditer(text) if "." in m.group(1))
    models.update(m.group(1) for m in M2O_RE.finditer(text) if "." in m.group(1))

    fields = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return models, fields

    class Visitor(ast.NodeVisitor):
        current_model = None

        def visit_Assign(self, node):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in {"_name", "_inherit"} and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    self.current_model = node.value.value
            self.generic_visit(node)

        def visit_Call(self, node):
            # search domains [('field', ...)]
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"search", "search_count", "read_group"} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.List):
                    for elt in arg.elts:
                        if isinstance(elt, ast.Tuple) and elt.elts and isinstance(elt.elts[0], ast.Constant) and isinstance(elt.elts[0].value, str):
                            fields.append((self.current_model or "unknown", elt.elts[0].value, "python-domain", path))
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"write", "create"} and node.args:
                payload = node.args[0]
                if isinstance(payload, ast.Dict):
                    for key in payload.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            fields.append((self.current_model or "unknown", key.value, "python-write", path))
            self.generic_visit(node)

    Visitor().visit(tree)
    return models, fields


def parse_xml(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    fields = []
    model = "unknown"
    for line in text.splitlines():
        if "model=\"ir.ui.view\"" in line:
            model = "view"
        for m in XML_FIELD_RE.finditer(line):
            fields.append((model, m.group(1), "xml-view", path))
    return set(), fields


def parse_js(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    models = set(m.group(1) for m in ENV_MODEL_RE.finditer(text) if "." in m.group(1))
    fields = [("unknown", m.group(1), "js-token", path) for m in JS_FIELD_RE.finditer(text) if len(m.group(1)) > 2 and " " not in m.group(1)]
    return models, fields


def main():
    used_models = set()
    used_fields = []
    for path in iter_files():
        if path.suffix == ".py":
            models, fields = parse_py(path)
        elif path.suffix == ".xml":
            models, fields = parse_xml(path)
        else:
            models, fields = parse_js(path)
        used_models.update(models)
        used_fields.extend(fields)

    (OUT_DIR / "used_models.txt").write_text("\n".join(sorted(used_models)) + "\n", encoding="utf-8")

    rows = ["model,field,context,source"]
    for model, field, context, source in sorted({(m, f, c, str(s.relative_to(ROOT))) for m, f, c, s in used_fields}):
        rows.append(f"{model},{field},{context},{source}")
    (OUT_DIR / "used_fields.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")

    (OUT_DIR / "extract_summary.json").write_text(
        json.dumps({"models": len(used_models), "fields": len(rows) - 1}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
