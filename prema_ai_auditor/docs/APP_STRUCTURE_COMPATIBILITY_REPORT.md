# App Structure, Field Mapping, and Compatibility Report

## Scope
This document validates the `prema_ai_auditor` module against the exported runtime reference snapshots in `/reference` and summarizes architecture compatibility for Odoo 18 production usage.

Reference files used:
- `reference/01_all_models.sql`
- `reference/02_all_fields.sql`
- `reference/05_system_parameters.sql`
- `reference/06_installed_modules.sql`
- `reference/08_odoo_config.txt`
- `reference/09_odoo_workers.txt`

## Application Structure Tree
```text
prema_ai_auditor/
├── controllers/                 # HTTP endpoints for chat/upload flows
├── data/                        # XML data seeds (rules + cron)
├── docs/                        # Generated and manual technical reports
├── models/                      # ORM models, engines, and wizards
│   └── wizards/                 # Interactive config/schema helper wizards
├── reference/                   # Production snapshot exports used for validation
├── scripts/                     # Offline scanners and report generators
├── security/                    # Access control + model security policies
├── services/                    # Service-layer abstractions (LLM/config/tools/perf)
├── static/src/                  # Front-end JS/XML/SCSS assets for backend UI widgets
├── views/                       # Odoo backend XML views/actions/menus
├── __manifest__.py              # Odoo module metadata + dependencies + assets
└── __init__.py                  # Python package bootstrap
```

## What Each Functional Area Does
- **Audit and integrity engines (`models/`)**: analyze accounting and operational objects, log findings, and prepare proposal/write-gated actions.
- **LLM orchestration (`services/llm_service.py`, `services/openai_client.py`)**: performs guarded API calls with rate-limiting and circuit-breaker-like failure tracking.
- **Document AI (`models/ai_document*.py`)**: extraction + classification + draft creation pipeline for uploaded accounting documents.
- **Monitoring (`models/ai_error_monitor.py`, `models/ai_performance_monitor.py`, dashboards)**: tracks failures and health indicators for admin observability.
- **Safety gates (`models/prema_ai_proposal.py`, `models/ai_write_gate.py`)**: separates advice generation from direct writes, requiring explicit flow to apply changes.
- **Reference validation (`models/mapping_validator.py`, `scripts/full_audit_scan.py`)**: compares used models/fields versus exported reference snapshots.

## Field/Model Mapping Check Result
### 1) Core Odoo model compatibility (against `reference/01_all_models.sql`)
- All detected **core Odoo models used by code** are present in reference exports.
- Checked models include: `account.move`, `account.move.line`, `account.account`, `res.partner`, `mail.mail`, `ir.logging`, `ir.cron`, `ir.config_parameter`, etc.
- Result: **PASS** for core mapping.

### 2) Custom `prema.*` model compatibility
- `prema.*` models are naturally absent from base model exports unless module tables are installed in that source snapshot.
- Internal cross-check confirms custom model references resolve to declared `_name` definitions inside this module.
- Result: **PASS** for internal model mapping consistency.

### 3) Field mapping source
- Field source file remains `reference/02_all_fields.sql` and is consumed by `models/mapping_validator.py`.
- The validator implementation is aligned to model|field|type|relation parsing and is suitable for static drift detection.

## AI/MLL Strength vs Server Load Review
Current implementation already includes useful load guards:
- LLM per-user rate limit gate (`prema.performance.guard`): `max_calls=20` per `60s` window.
- External call timeout + retry (`prema.openai.client`): timeout `45s`, retries `2`.
- Circuit breaker style stop after repeated failures via system parameter (`prema_ai_auditor.llm_failure_count`).
- Batch processing for pending docs with explicit `limit=20` (`process_pending_documents`).

### Load risks to track
- A few searches are unbounded in scanners/analyzers (`search([])` and full-domain scans) and may become heavy on very large datasets.
- No `@api.depends` compute graph loops detected; no direct compute recursion risk found in current static scan.

## Server / System / Database Compatibility (Static)
- Module target is Odoo 18 with ORM usage and no raw SQL calls detected.
- Manifest dependencies (`account`, `sale`, `purchase`, `mail`, `web`, etc.) are standard Odoo apps and structurally compatible with Ubuntu/Python deployment stack.
- Configuration pattern uses `ir.config_parameter` first, then environment fallback, which is deployment-safe.
- No hardcoded DB IDs observed in critical paths reviewed.

## Operational Recommendation (Production-safe)
1. Keep current LLM limits enabled in production (`performance.guard` + circuit breaker).
2. Add explicit limits/pagination to remaining full-table scans before high-volume rollout.
3. Continue using `/reference` exports for periodic drift scans after each module upgrade.
4. Re-run `scripts/full_audit_scan.py` after any model/view change and archive updated report artifacts in `/docs`.

## Conclusion
- **Mapping status:** compatible for core models and internally consistent for custom `prema.*` models.
- **Performance safety:** mostly guarded; some non-critical full scans should be bounded for very large production databases.
- **Platform compatibility:** compatible with Odoo 18 ORM architecture and standard production service layout.
