# PremaFirm AI Engine (Odoo 18)

PremaFirm AI Engine is an Odoo module that connects CRM lead intake, dispatch planning, pricing logic, run planning, sales order creation, and invoice alignment for logistics workflows.

---

## Application manual

## 1) What this app does

This module extends Odoo to support an end-to-end freight lifecycle:

1. Capture a load opportunity in **CRM**.
2. Add dispatch stops (pickup/delivery, pallets, weights, schedule windows).
3. Compute routing + timing + pricing suggestions.
4. Assign service products per stop (FTL/LTL, country-aware defaults).
5. Create a Sales Order from CRM with stop-based lines.
6. Apply adjustment/final-rate logic.
7. Plan dispatch runs and create calendar bookings.
8. Generate POD reporting and align accounting behavior by region.

---

## 2) Functional modules and behavior

### A) CRM + dispatch orchestration
- Maintains dispatch stops tied to leads.
- Computes lead-level totals (distance, load metrics, estimated cost/suggested rate).
- Supports dynamic service/load type behavior and stop-level product assignment.

### B) Mapbox + ETA estimation
- Uses geocoding + routing for stop chains including vehicle home base.
- Stores leg distance/time and map links.
- Supports ETA sequencing and lead-level aggregate totals.

### C) Pricing engine and rule handling
- Calculates estimated operating cost and suggested rate.
- Applies rule-based decision outputs (for example overload rejection paths).
- Tracks historical pricing artifacts.

### D) Sales + accounting handoff
- Creates Sales Orders directly from CRM lead context.
- Maps stop data into order lines and pushes adjustment logic.
- Extends account move behavior for region/company/journal choices.

### E) Run planning + booking
- Dispatch run model supports lifecycle status and metrics.
- Planner updates run metrics and calendar events.
- Booking model computes duration and validates scheduling overlap behavior.

### F) AI extraction/logging support
- Provides AI extraction service integration path.
- Stores AI log records and AI-related helper models.

---

## 3) Installation and dependencies

Defined in manifest:
- Required modules: `crm`, `sale_management`, `account`, `mail`, `fleet`, `hr`, `calendar`.
- Data loaded: security ACL, load sequence data, dispatch rules, CRM/dispatch/sale/account/report views.

Production hardening notes:
- `billing_mode` is permanently removed from business logic and migration cleanup is idempotent for `crm.lead`, `sale.order`, `premafirm.ai.log`, and `premafirm.load`.
- Dispatch product resolution is strict by normalized country (`CA`/`US`), load type (`FTL`/`LTL`), and reefer flag; unresolved mappings raise `UserError`.
- Service products are never auto-created by this module; only existing products are selected.

Install as a standard custom module in Odoo 18 and update apps list.

Timezone note:
- Python `zoneinfo` is used for schedule calculations. On minimal Linux images, install timezone data with `pip install tzdata` if system tzdata is unavailable.


Deployment notes:
- Validate Python timezone support in the target environment before go-live, e.g. `python -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Toronto')"`.
- Keep production Odoo settings at `log_level = info` and ensure no `debug=True` flags are enabled.
- Store external API keys (Mapbox and Weather provider) in `ir.config_parameter` and verify they are present during deployment checks.

---

## 4) Developer workflow

### Run Python tests in this repository
- Root-level unit-style tests:
  - `pytest tests/test_dispatch_service.py`
  - `pytest tests/test_booking_requirements.py`
  - `pytest tests/test_ai_dispatch_requirements.py`
- Odoo TransactionCase tests are located under `premafirm_ai_engine/tests/` and are intended to run in an Odoo test environment.

### Odoo 18 compatibility conventions
- Use `<list>` for list views (not `<tree>`).
- Prefer stable ID-based XPath targets in report inheritance.

---

## 5) File-by-file reference (what each file is for)

### Root
- `README.md` — This manual and repository reference.
- `tests/test_ai_dispatch_requirements.py` — Unit tests for AI/dispatch requirement behavior and helper extraction logic.
- `tests/test_dispatch_service.py` — Unit tests for dispatch service lead total computations and rule outcomes.
- `tests/test_booking_requirements.py` — Unit tests for booking onchange/duration logic and lightweight Odoo stubs.

### Module package: `premafirm_ai_engine/`
- `premafirm_ai_engine/__init__.py` — Initializes module Python packages (`models`, `services`).
- `premafirm_ai_engine/__manifest__.py` — Odoo addon metadata, dependencies, and loaded XML/CSV data.

### Module tests: `premafirm_ai_engine/tests/`
- `premafirm_ai_engine/tests/__init__.py` — Registers Odoo test modules.
- `premafirm_ai_engine/tests/test_run_planner_service.py` — TransactionCase tests for run updates and calendar event creation.
- `premafirm_ai_engine/tests/test_crm_lead_product_assignment.py` — TransactionCase tests for stop product assignment (FTL/LTL by scenario).

### Models: `premafirm_ai_engine/models/`
- `models/__init__.py` — Imports model extensions and model-layer service bridge.
- `models/dispatch_stop.py` — Dispatch stop model (sequence, stop type, scheduling, routing/pallet fields, product/service mapping).
- `models/dispatch_run.py` — Dispatch run header model (vehicle, run date, status, timing, metrics, calendar link).
- `models/pricing_history.py` — Persists pricing calculation snapshots/history.
- `models/crm_lead_extension.py` — Extends `crm.lead` with dispatch, pricing, scheduling, and sales-order orchestration logic.
- `models/fleet_vehicle_extension.py` — Extends fleet vehicle fields used by routing/service/load planning.
- `models/sale_order_extension.py` — Extends sales order behavior/fields used by PremaFirm handoff and POD flow.
- `models/account_move_extension.py` — Extends invoice/account move defaults and regional handling.
- `models/mail_compose_message.py` — Extends mail compose behavior for load/document communication flows.
- `models/ai_engine.py` — AI engine model/controller layer for extraction/automation entry points.
- `models/ai_log.py` — AI logging model for request/response tracking and auditability.
- `models/premafirm_load.py` — Load-level model for dispatch planning and operational state.
- `models/premafirm_booking.py` — Booking model for driver/vehicle scheduling and duration calculations.
- `models/res_partner_extension.py` — Partner/customer extensions used in dispatch/accounting decisions.

### Services: `premafirm_ai_engine/services/`
- `services/__init__.py` — Service package exports.
- `services/dispatch_service.py` — Core dispatch totals engine (distance, pallets, weight, cost/rate, decision helpers).
- `services/crm_dispatch_service.py` — CRM-facing scheduling/ETA/business-rule orchestration.
- `services/mapbox_service.py` — Geocoding/routing helpers and map link generation using Mapbox APIs.
- `services/pricing_engine.py` — Pricing calculations and strategy helpers.
- `services/dispatch_rules_engine.py` — Structured dispatch rule evaluation.
- `services/ai_extraction_service.py` — AI document/email extraction service logic.
- `services/run_planner_service.py` — Run planning and run/calendar update routines.

### Security: `premafirm_ai_engine/security/`
- `security/ir.model.access.csv` — Access control list entries for custom models.

### Data seeds/config: `premafirm_ai_engine/data/`
- `data/product_data.xml` — Product/service seed data for dispatch billing lines.
- `data/load_sequence.xml` — Sequence definitions for load/run identifiers.
- `data/dispatch_rules.yaml` — Human-editable dispatch rule definitions.
- `data/dispatch_rules.json` — JSON-form dispatch rules (runtime/compatibility source).

### Views/reports: `premafirm_ai_engine/views/`
- `views/crm_view.xml` — CRM lead UI extensions (dispatch, pricing, actions).
- `views/dispatch_stop_views.xml` — Dispatch stop list/form views and interactions.
- `views/sale_order_view.xml` — Sales order UI customizations for PremaFirm workflow.
- `views/premafirm_load_view.xml` — PremaFirm load model list/form/search views.
- `views/premafirm_booking_views.xml` — Booking UI views and scheduling interactions.
- `views/account_move_view.xml` — Invoice/account move UI customizations.
- `views/report_premafirm_pod.xml` — POD QWeb report template.
- `views/report_company_header_inherit.xml` — Company header/report inheritance customization.

---

## 6) Suggested maintenance checklist

- Keep tests in `tests/` runnable without full Odoo boot for service/model utility logic.
- Keep Odoo TransactionCase tests in `premafirm_ai_engine/tests/` for integration behavior.
- When adding new features, update:
  1) model/service code,
  2) view XML,
  3) security ACL if new model,
  4) this README file map/manual.
