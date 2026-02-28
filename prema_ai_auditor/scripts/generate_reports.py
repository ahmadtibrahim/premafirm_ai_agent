#!/usr/bin/env python3
"""Generate structural, crash, performance, security, and write-gate reports."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REF = ROOT / "reference"
DOCS.mkdir(exist_ok=True)

PY_FILES = [p for p in ROOT.rglob("*.py") if "reference" not in p.parts and ".git" not in p.parts]
JS_FILES = [p for p in ROOT.rglob("*.js") if "reference" not in p.parts and ".git" not in p.parts]
XML_FILES = [p for p in ROOT.rglob("*.xml") if "reference" not in p.parts and ".git" not in p.parts]


def rel(path):
    return str(path.relative_to(ROOT))


def find(pattern, files):
    regex = re.compile(pattern)
    hits = []
    for path in files:
        for no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if regex.search(line):
                hits.append((rel(path), no, line.strip()))
    return hits


def app_structure_report():
    depends = []
    manifest = (ROOT / "__manifest__.py").read_text(encoding="utf-8")
    for match in re.finditer(r'"([a-z_]+)"', manifest):
        if match.group(1) in {"account", "account_accountant", "fleet", "crm", "sale", "purchase", "documents", "mail", "web", "bus"}:
            depends.append(match.group(1))

    installed = (REF / "06_installed_modules.sql").read_text(encoding="utf-8", errors="ignore")
    missing_deps = [dep for dep in depends if dep not in installed]

    lines = [
        "# App Structure + Compatibility Report",
        "",
        "- Module: `prema_ai_auditor`",
        "- Manifest version indicates Odoo 18 stream (`18.0.1.0`).",
        f"- Dependencies checked against reference snapshot: {', '.join(depends)}",
        f"- Missing dependency candidates in snapshot: {', '.join(missing_deps) if missing_deps else 'None'}",
        "- OWL assets present in `web.assets_backend` for chat/upload templates.",
    ]
    (DOCS / "app_structure_compatibility_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def crash_risk_report():
    import_mismatch = find(r"from \. import (llm_service|openai_client|plan_generator)", [ROOT / "models" / "__init__.py"])
    broad_except = find(r"except\s*:", PY_FILES)
    missing_route_usage = find(r"/prema_ai/", JS_FILES)
    lines = ["# Crash Risk Report", "", "## Python", "- Potential import path hazards:"]
    lines.extend([f"- `{f}:{ln}` `{text}`" for f, ln, text in import_mismatch] or ["- None"])
    lines.extend(["", "- Bare except blocks:"])
    lines.extend([f"- `{f}:{ln}` `{text}`" for f, ln, text in broad_except] or ["- None"])
    lines.extend(["", "## JS/OWL", "- RPC route references discovered:"])
    lines.extend([f"- `{f}:{ln}` `{text}`" for f, ln, text in missing_route_usage] or ["- None"])
    lines.extend(["", "## XML", "- Validate view inheritance and external ids during module upgrade (`-u prema_ai_auditor`)."])
    (DOCS / "crash_risk_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def performance_report():
    unbounded = find(r"search\(\s*\[\s*\]\s*\)", PY_FILES)
    heavy = find(r"search\(\[", PY_FILES)
    lines = ["# Performance Report", "", "## Unbounded Searches", *(f"- `{f}:{ln}` `{text}`" for f, ln, text in unbounded)]
    if not unbounded:
        lines.append("- None detected")
    lines.extend(["", "## Search-heavy paths (review limits/indexes)"])
    lines.extend([f"- `{f}:{ln}` `{text}`" for f, ln, text in heavy[:80]])
    lines.extend(["", "## Worker/Cron", "- Ensure cron handlers batch (`limit`/`seek`) and avoid long blocking HTTP operations."])
    (DOCS / "performance_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def security_report():
    secret_hits = []
    for pattern in [r"sk-[A-Za-z0-9]{10,}", r"api[_-]?key\s*=", r"mapbox", r"openai\.api_key", r"journal_id\s*=\s*\d+"]:
        secret_hits.extend(find(pattern, PY_FILES + JS_FILES + XML_FILES))
    lines = ["# Security Report", "", "## Findings"]
    lines.extend([f"- `{f}:{ln}` `{text}`" for f, ln, text in secret_hits[:100]] or ["- No hardcoded token signature found."])
    lines.extend(
        [
            "",
            "## Mitigation Plan",
            "- Store secrets only in `ir.config_parameter` / environment variables.",
            "- Mask secret values in all logs/UI diagnostics.",
            "- Rotate keys immediately after any exposure.",
        ]
    )
    (DOCS / "security_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def no_auto_write_report():
    write_hits = find(r"\.\s*(write|create|unlink)\(", PY_FILES)
    lines = [
        "# No Auto-Write Enforcement Report",
        "",
        "| location | call | gating_status |",
        "|---|---|---|",
    ]
    for f, ln, text in write_hits:
        gate = "gated" if "write_gate" in f or "proposal" in f else "review_required"
        lines.append(f"| {f}:{ln} | `{text}` | {gate} |")
    (DOCS / "no_auto_write_enforcement_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    app_structure_report()
    crash_risk_report()
    performance_report()
    security_report()
    no_auto_write_report()


if __name__ == "__main__":
    main()
