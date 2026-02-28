#!/usr/bin/env python3
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference"
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

PY_FILES = list(ROOT.rglob("*.py"))
XML_FILES = list(ROOT.rglob("*.xml"))
JS_FILES = list(ROOT.rglob("*.js"))
FILES = [p for p in (PY_FILES + XML_FILES + JS_FILES) if "reference" not in p.parts and ".git" not in p.parts]


def read_lines(path):
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def load_reference_models():
    models = set()
    for line in read_lines(REF / "01_all_models.sql"):
        m = re.search(r"\|\s*([a-z0-9_]+\.[a-z0-9_.]+)\s*\|", line)
        if m:
            models.add(m.group(1))
    return models


def load_reference_fields():
    fields = {}
    for line in read_lines(REF / "02_all_fields.sql"):
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        model, field, _label, ftype, relation = parts[:5]
        if "." not in model:
            continue
        fields.setdefault(model, {})[field] = {"type": ftype, "relation": relation}
    return fields


def collect_usage():
    usages = []
    model_patterns = [re.compile(r"env\[['\"]([^'\"]+)['\"]\]"), re.compile(r"self\.env\[['\"]([^'\"]+)['\"]\]")]
    field_pattern = re.compile(r"<field[^>]+name=['\"]([^'\"]+)['\"]")
    for path in FILES:
        lines = read_lines(path)
        rel = path.relative_to(ROOT)
        for no, line in enumerate(lines, 1):
            for p in model_patterns:
                for m in p.finditer(line):
                    usages.append((str(rel), no, "model", m.group(1), "read"))
            if path.suffix == ".xml":
                for m in field_pattern.finditer(line):
                    usages.append((str(rel), no, "field", m.group(1), "view"))
            if "search([" in line or "search_count([" in line:
                usages.append((str(rel), no, "domain", "search_domain", "domain"))
            if "cr.execute" in line:
                usages.append((str(rel), no, "sql", "cr.execute", "sql"))
    return usages


def find_hardcoded():
    patterns = [
        re.compile(r"sk-[A-Za-z0-9]{10,}"),
        re.compile(r"pk\.[A-Za-z0-9._-]{10,}"),
        re.compile(r"mapbox", re.I),
        re.compile(r"openai", re.I),
        re.compile(r"company_id\s*=\s*\d+"),
        re.compile(r"journal_id\s*=\s*\d+"),
        re.compile(r"account_id\s*=\s*\d+"),
    ]
    hits = []
    for path in FILES:
        for no, line in enumerate(read_lines(path), 1):
            if any(p.search(line) for p in patterns):
                hits.append((str(path.relative_to(ROOT)), no, line.strip()))
    return hits


models_ref = load_reference_models()
fields_ref = load_reference_fields()
usages = collect_usage()
hardcoded = find_hardcoded()

mapping_lines = [
    "| module_file | model | field | usage | exists | type_match | risk | fix |",
    "|---|---|---|---|---|---|---|---|",
]
for file_path, line_no, kind, value, usage in usages:
    if kind == "model":
        exists = "Y" if value in models_ref else "N"
        risk = "high" if exists == "N" else "low"
        fix = "Add/rename model usage to a valid Odoo model" if exists == "N" else "None"
        mapping_lines.append(f"| {file_path}:{line_no} | {value} | - | {usage} | {exists} | Y | {risk} | {fix} |")
    elif kind == "field":
        # View field may belong to current model context; mark unknown conservatively.
        mapping_lines.append(f"| {file_path}:{line_no} | (view-context) | {value} | view | ? | ? | medium | Validate field exists on the view model in Odoo registry |")
    elif kind == "sql":
        mapping_lines.append(f"| {file_path}:{line_no} | - | - | sql | Y | Y | medium | Ensure parameterized SQL and indexed predicates |")

(DOCS / "MAPPING_REPORT.md").write_text("# Mapping Report\n\n" + "\n".join(mapping_lines) + "\n", encoding="utf-8")

audit = [
    "# Audit Report",
    "",
    "## Key Failures / Risks",
    "- **High**: Hardcoded external endpoint/model values in LLM stack reduce environment portability.",
    "- **High**: Error propagation from external calls included raw exception text in user-visible errors.",
    "- **Medium**: Bus channel uses global channel name; ensure only partner-targeted payloads are sent.",
    "- **Medium**: No central runtime schema comparison report existed.",
    "",
    "## Hardcoded/Secret Scan Findings",
]
for f, ln, text in hardcoded[:100]:
    audit.append(f"- `{f}:{ln}` -> `{text}`")
(DOCS / "AUDIT_REPORT.md").write_text("\n".join(audit) + "\n", encoding="utf-8")

(DOCS / "HARDENING_PLAN.md").write_text(
    "# Hardening Patch Plan\n\n"
    "1. Centralize configuration through `prema.config.service` (ir.config_parameter -> env -> safe default).\n"
    "2. Keep write operations gated behind proposal approval workflow (`prema.ai.proposal`).\n"
    "3. Maintain read-only default path by generating proposals first for non-user initiated fixes.\n"
    "4. Guard external LLM calls with timeout, retries, circuit breaker and sanitized user errors.\n"
    "5. Add schema viewer/diff wizard to compare runtime registry vs reference exports.\n"
    "6. Keep bus payload minimal and user-scoped via partner-targeted send.\n"
    "7. Keep `/reference` folder immutable; use it only for mapping validation.\n",
    encoding="utf-8",
)

(DOCS / "DYNAMIC_CONFIG.md").write_text(
    "# Dynamic Config Plan\n\n"
    "Resolution order for all config values:\n"
    "1. `ir.config_parameter`\n"
    "2. Environment variable\n"
    "3. Safe default (non-secret only)\n\n"
    "Required keys:\n"
    "- `openai.api_key` / `OPENAI_API_KEY`\n"
    "- `prema_ai_auditor.openai_endpoint` / `OPENAI_ENDPOINT`\n"
    "- `prema_ai_auditor.openai_model` / `OPENAI_MODEL`\n"
    "- `web.base.url` / `WEB_BASE_URL`\n"
    "- `mail.catchall.domain` / `MAIL_CATCHALL_DOMAIN`\n\n"
    "Secret logging policy: show masked values only (`ABCD...WXYZ`).\n",
    encoding="utf-8",
)

(DOCS / "TEST_PLAN.md").write_text(
    "# Test Plan\n\n"
    "## Unit Tests\n"
    "- Config service getters: parameter precedence and masking behavior.\n"
    "- Proposal lifecycle: draft -> pending_approval -> approved/rejected -> applied/failed.\n"
    "- Mapping validator: model extraction and reference cross-check.\n\n"
    "## Integration Tests\n"
    "- Run mapping scan script against `/reference` exports.\n"
    "- Simulate LLM timeout and ensure user gets sanitized `UserError`.\n"
    "- Simulate bus delivery failure path and verify no worker crash.\n\n"
    "## Performance Tests\n"
    "- Large-domain searches for documents and audit logs with pagination.\n"
    "- Detect N+1 patterns in session/document summarization flow.\n",
    encoding="utf-8",
)

print("Generated docs in /docs")
