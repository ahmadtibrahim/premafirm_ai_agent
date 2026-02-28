import re
from pathlib import Path

from odoo import models


class PremaMappingValidator(models.AbstractModel):
    _name = "prema.mapping.validator"
    _description = "Prema Mapping Validator"

    def _reference_fields_for_model(self, model_name):
        repo_root = Path(__file__).resolve().parents[1]
        fields_file = repo_root / "reference" / "02_all_fields.sql"
        rows = []
        if not fields_file.exists():
            return rows
        for line in fields_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            if parts[0] == model_name:
                rows.append({"model": parts[0], "field": parts[1], "type": parts[3], "relation": parts[4]})
        return rows

    def scan_repository_references(self):
        repo_root = Path(__file__).resolve().parents[1]
        patterns = [
            (re.compile(r"env\[['\"]([^'\"]+)['\"]\]"), "model"),
            (re.compile(r"self\.env\[['\"]([^'\"]+)['\"]\]"), "model"),
            (re.compile(r"<field[^>]+name=['\"]([^'\"]+)['\"]"), "view_field"),
        ]
        findings = []
        for file_path in repo_root.rglob("*"):
            if file_path.is_dir() or "reference" in file_path.parts:
                continue
            if file_path.suffix not in {".py", ".xml", ".js"}:
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            rel = str(file_path.relative_to(repo_root))
            for pattern, usage in patterns:
                for match in pattern.finditer(text):
                    findings.append({"file": rel, "usage": usage, "value": match.group(1)})
        return findings
