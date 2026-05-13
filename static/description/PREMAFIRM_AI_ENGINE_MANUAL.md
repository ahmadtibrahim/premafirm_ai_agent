# PREMAFIRM AI Engine — Module Manual
**Technical Name:** `premafirm_ai_engine`
**Version:** 18.0.6.5.0
**Author:** PremaFirm
**License:** LGPL-3
**Platform:** Odoo 18 (erp.premafirm.com)

---

## What This Module Does

The PremaFirm AI Engine is a custom Odoo module that turns the CRM, Invoicing, Fleet, and Documents apps into an intelligent freight sales and operations platform for **PREMAFIRM INC.**, a federally authorized trucking carrier based in Mississauga, Ontario, Canada.

The module integrates:
- **Claude / OpenAI API** — AI reasoning and draft generation
- **Snov.io API** — Contact discovery and email verification
- **Geotab API** — ELD telematics, driver logs, odometer, fuel data
- **Fleetbase API** — Dispatch order creation and tracking
- **MapBox API** — Geocoding and truck-profile routing
- **Google Places API** — Address autocomplete

---

## Equipment & Carrier Profile

| Field | Value |
|---|---|
| Truck | 26FT Freightliner M2 Straight Truck |
| Capability | Reefer & Dry |
| Payload | ~14,000 lbs / ~12 pallets |
| Liftgate | Yes |
| Suspension | Air Ride |
| USDOT | 4512323 |
| MC | 1786607 |
| CVOR | 227-065-594 |
| SCAC | PSHL |
| Canadian Carrier Code | 12LG |
| ELD | Geotab (compliant) |
| Insurance | $2M liability / $100K cargo / Reefer breakdown |

---

## Module Structure

```
premafirm_ai_engine/
├── models/
│   ├── crm_ai_assistant.py        ← AI Chat Box, Compose Email fix, Account Summary
│   ├── crm_lead_extension.py      ← Lead fields, email segment routing
│   ├── crm_lead_ml.py             ← ML learning hooks on leads
│   ├── crm_followup.py            ← Follow-up cron + cold reactivation
│   ├── crm_contact_rotation.py    ← Snov.io escalation + contact rotation
│   ├── crm_bulk_email.py          ← Bulk email batching with delay queue
│   ├── business_profile.py        ← Company identity + ICP singleton
│   ├── ml_engine.py               ← Core AI engine (GPT + RAG)
│   ├── ml_knowledge.py            ← Knowledge base model
│   ├── ml_draft.py                ← Draft review + learning loop
│   ├── ml_ingestion.py            ← Bulk historical data ingestion
│   ├── ml_ingest_queue.py         ← Async ML ingest queue
│   ├── ml_orm_hooks.py            ← ORM event hooks → knowledge queue
│   ├── ml_response_cache.py       ← SHA-256 AI response cache
│   ├── ml_learning_hooks.py       ← Confirmed action learning
│   ├── rate_estimator.py          ← Trip cost engine + Fleetbase dispatch
│   ├── estimator_stop.py          ← Stop model for rate estimator
│   ├── dispatch_wizard.py         ← AI-powered dispatch job wizard
│   ├── account_move_extension.py  ← Invoice fields + AI generate
│   ├── account_move_ml.py         ← Bill ML hooks + IFTA
│   ├── fleet_vehicle_extension.py ← Truck fields + telematics
│   ├── geotab_sync.py             ← Geotab sync engine (vehicles/drivers/fuel)
│   ├── geotab_settings.py         ← Geotab + Fleetbase settings
│   ├── ifta_fuel_log.py           ← IFTA fuel reporting model
│   ├── premafirm_load.py          ← POD / Load records
│   ├── res_partner_extension.py   ← Contact fields + Geotab driver link
│   ├── res_partner_ml.py          ← Auto-tagging + AI tag suggestions
│   ├── sale_order_extension.py    ← Sale order fields + AI generate quote
│   ├── snov_contact.py            ← Snov.io prospect contact model
│   ├── snov_search_wizard.py      ← Snov.io domain search wizard
│   ├── documents_ml.py            ← AI bill processing from Documents folder
│   ├── bill_scan_import.py        ← Bill scan import tracking
│   ├── invoice_ai_product.py      ← AI-selectable invoice products
│   └── mail_compose_message.py    ← Email composer AI draft override
│
├── services/
│   ├── openai_utils.py            ← OpenAI HTTP helper with retry
│   ├── claude_utils.py            ← Anthropic Claude API helper
│   ├── snov_service.py            ← Snov.io API client
│   ├── geotab_service.py          ← Geotab API client
│   ├── fleetbase_service.py       ← Fleetbase API client
│   ├── mapbox_service.py          ← MapBox geocoding + routing
│   ├── pricing_engine.py          ← Trip cost calculation
│   ├── invoice_ai_service.py      ← Invoice AI extraction
│   ├── bill_scan_service.py       ← Bill scan AI service
│   ├── dispatch_document_service.py ← Route sheet parser
│   └── document_extractor.py     ← Zero-cost PDF/OCR text extraction
│
└── views/
    ├── crm_ai_assistant_views.xml ← AI Assistant tab on CRM leads
    ├── crm_view.xml               ← CRM buttons + Google autocomplete
    ├── crm_bulk_email_views.xml   ← Bulk email batch views
    ├── business_profile_views.xml ← Business profile form
    ├── rate_estimator_view.xml    ← Dispatch estimate list + form
    ├── dispatch_wizard_view.xml   ← Dispatch job wizard
    ├── account_move_view.xml      ← Invoice AI buttons
    ├── fleet_vehicle_geotab_view.xml ← Truck telematics + Geotab tabs
    ├── res_partner_driver_view.xml   ← Driver profile tab
    ├── snov_search_wizard_view.xml   ← Prospect search wizard
    ├── bill_scan_import_views.xml    ← Bill scan imports list
    ├── ifta_fuel_log_views.xml       ← IFTA fuel log
    ├── ml_knowledge_views.xml        ← Knowledge base admin views
    ├── ml_draft_views.xml            ← Draft review views
    ├── ml_ingestion_views.xml        ← Data ingestion views
    └── geotab_settings_view.xml      ← Settings page
```

---

## Feature Reference

---

### 1. CRM AI Sales Assistant
**File:** `models/crm_ai_assistant.py`
**Appears on:** CRM Lead form → "AI Assistant" tab

#### Functions

| Function | Description |
|---|---|
| `action_ai_chat_send()` | Sends the user's question to AI. Reads full lead context (email thread + company notes + contact notes) before responding. |
| `action_ai_compose_email()` | Opens Odoo email composer pre-filled with AI draft. Subject auto-generated from lead/company name. Body stripped of any accidental subject lines AI may include. Contact pre-filled in To field. Threaded to CRM lead chatter. |
| `action_ai_append_company()` | Appends AI response to the **company** Log Note (for freight/lane/rate info that belongs to the company record, not the individual). |
| `action_ai_append_contact()` | Appends AI response to the **contact's** Log Note (for personal preferences, communication style, individual notes). |
| `action_ai_append_lead()` | Appends AI response as a note directly on the CRM lead. |
| `_ai_system_prompt()` | Builds the master AI personality and rule set. Includes full PREMAFIRM profile, equipment specs, compliance numbers, seasonal awareness, and email rules (no subject line in body, no signature). |
| `_ai_lead_context()` | Compiles everything the AI reads before responding: lead fields, last 8 email messages, contact notes, company notes. |
| `_ai_company_partner()` | Returns the company-level partner (parent) for a contact, used to determine where to log company-level notes. |
| `_auto_log_reply()` | Auto-logs incoming email replies to both company log and contact log. |
| `message_post()` (override) | Detects outgoing vs incoming emails. Stamps `x_last_outreach_at` on outgoing, sets `x_response_status = replied` on incoming. |

#### Fields Added to `crm.lead`
| Field | Type | Purpose |
|---|---|---|
| `x_ai_chat_input` | Text | User types question or request here |
| `x_ai_chat_response` | Text | AI response displayed here (readonly) |
| `x_followup_1_sent_at` | Datetime | Timestamp when follow-up 1 was drafted |
| `x_followup_2_sent_at` | Datetime | Timestamp when follow-up 2 was drafted |
| `x_last_outreach_at` | Datetime | Timestamp of last outgoing email |
| `x_response_status` | Selection | none / replied / bounced / unsubscribed |
| `x_rotation_count` | Integer | How many contacts attempted at this company |
| `x_snov_enrichment_requested` | Boolean | Prevents duplicate Snov.io API calls |

#### Fix Log
- **May 13 2026:** `action_ai_compose_email` — Added `default_subject` (auto-built from lead/company name). Fixed `default_res_id` (singular) alongside `default_res_ids` for correct chatter threading. Added `default_partner_ids` to pre-fill To field. Added regex stripping of "Subject:" lines AI may write in body.
- **May 13 2026:** `_ai_system_prompt` — Added explicit rule: AI must NOT write a subject line in the email body.

---

### 2. Won / Lost Debrief + Onboarding Checklist
**File:** `models/crm_ai_assistant.py`

| Function | Description |
|---|---|
| `action_set_won()` | Triggers `_ai_won_debrief()`. AI writes a 3-sentence debrief of what worked and what was agreed. Posted to company Log Note. Also auto-posts carrier onboarding checklist to the lead. |
| `action_set_lost()` | Triggers `_ai_lost_debrief()`. AI writes a 2-sentence debrief of the objection and what to improve. Posted to company Log Note. |

**Carrier Onboarding Checklist (auto-posted on Won):**
- Carrier packet sent
- Insurance certificate received
- CVOR + FMCSA (if cross-border) verified
- Customer portal login created
- First load date confirmed
- Rate confirmation signed
- BOL template shared
- Dispatch + after-hours contact verified

---

### 3. Account Summary Generator
**File:** `models/crm_ai_assistant.py` → `ResPartnerAI`
**Appears on:** Company partner form → Smart button "Generate Summary"

| Function | Description |
|---|---|
| `action_generate_account_summary()` | Reads all contacts, leads, and company log notes. Produces structured summary: STATUS / PRIMARY CONTACT / LANE INTERESTS / LAST ACTIVITY / NEXT ACTION / RISK FLAGS / OPPORTUNITY SCORE. Saved to `x_account_summary` field on company record. |

---

### 4. Follow-up Workflow (Cron)
**File:** `models/crm_followup.py`
**Cron:** Daily at 07:00

| Function | Description |
|---|---|
| `run_followup_cron()` | Scans all leads with `x_response_status = none` and `x_last_outreach_at` set. After 3 business days: drafts Follow-up 1 (value-add angle) as a Log Note. After 5 more business days: drafts Follow-up 2 (mild urgency). Both posted as notes for Ahmad to review — never auto-sent. |
| `run_cold_reactivation_cron()` | Weekly scan. Leads cold for 60+ days get a reactivation draft posted. Includes seasonal context (e.g. produce season alert in spring). |
| `_post_followup(num, days)` | Builds the AI follow-up draft. Follow-up 1 = value-add angle. Follow-up 2 = urgency angle. Different message each time — not a copy-paste. |
| `_post_reactivation(seasonal_hint)` | Builds the reactivation email draft. References previous conversation briefly. |

---

### 5. Snov.io Contact Escalation
**File:** `models/crm_contact_rotation.py`
**Cron:** Daily (when enabled)

| Function | Description |
|---|---|
| `run_contact_rotation()` | Finds stale leads (no reply past threshold). Tries internal contacts first, then escalates to Snov.io. |
| `_process_lead_rotation(lead, icp_titles)` | Checks for untried contacts at same company ranked by ICP title match. Suggests best match as a Log Note for Ahmad to approve. |
| `_escalate_to_snov(lead, primary_partner, company_partner)` | Calls Snov.io API on company domain. Finds next best contact by title priority (VP/Director → Manager → Staff). Verifies email, deduplicates against existing Odoo contacts. Creates new contact + new Suggestion-stage lead + intro email draft. All logged to company, old contact, and new contact records. Ahmad approves everything — nothing sent automatically. |
| `action_snov_escalate_now()` | Manual trigger from lead form — bypasses cron timing. |

**Title Priority Scoring:**
1. VP, Director, C-Level
2. Manager, Head of, Supervisor
3. General contacts

---

### 6. Bulk Email Batch System
**File:** `models/crm_bulk_email.py`
**Appears on:** CRM list view → Actions → "Schedule Bulk Email"

| Model | Description |
|---|---|
| `premafirm.crm.bulk.email.batch` | Tracks a bulk send campaign (template, schedule, delay, state). |
| `premafirm.crm.bulk.email.queue` | One row per recipient email in a batch. Tracks sent/failed/bounced state. |
| `premafirm.crm.bulk.email.wizard` | Wizard to set template, timing, and delay between sends. |
| `run_bulk_email_cron()` | Processes the queue every minute. Sends one email at a time with configured delay. |

---

### 7. Trip Cost Estimator + Dispatch
**File:** `models/rate_estimator.py`, `services/pricing_engine.py`
**Appears on:** Customer Invoices → "Add Job / Dispatch" button + Estimator list

| Function | Description |
|---|---|
| `calculate_estimate()` | Full trip cost calculation. Inputs: vehicle, stops, weight, pallets. Resolves addresses via MapBox, routes via MapBox Directions API, calculates fuel/driver/maintenance/insurance costs, applies margin. Returns full cost breakdown + suggested rate. |
| `check_truck_availability_rpc()` | Checks Fleetbase schedule for conflicts. Returns availability status for each truck with next available windows. |
| `suggest_dispatch_plan_rpc()` | Picks best available truck + calculates estimate in one call. Used by dispatch wizard. |
| `action_dispatch_to_fleetbase()` | Creates Fleetbase order. Checks availability first. Links order ID to estimate record. |
| `extract_stops_from_file_rpc()` | Extracts stop list from uploaded route sheet (PDF/image). Tries local parsing first, falls back to AI vision if needed. |
| `add_system_stops()` | Adds system origin/return stops from truck GPS or home base. |

**Cost Parameters (configurable in System Parameters):**
| Parameter | Default |
|---|---|
| `estimator.fuel_price_per_l` | $1.55/L |
| `estimator.driver_rate_per_hr` | $40.00/hr |
| `estimator.margin_pct` | 20% |
| `estimator.weight_threshold_lbs` | 3,000 lbs |
| `estimator.weight_surcharge_per_cwt` | $5.00/CWT |

---

### 8. AI Invoice Generator
**File:** `models/account_move_extension.py`, `services/invoice_ai_service.py`
**Appears on:** Customer Invoice form → "AI Generate" button

| Function | Description |
|---|---|
| `action_ai_generate_invoice()` | Analyzes attachments (PDF/images) using AI. Extracts reference numbers (BOL, PO, Packing Slip), builds service description with route and date, selects appropriate product. Saves result to ML knowledge base. |
| `action_ai_extract_reference()` | Extracts only reference numbers from attachments. Zero-cost regex on text PDFs; AI vision only for scanned/image attachments. |
| `action_generate_ai_summary()` | Generates a plain-English AI summary of the invoice for internal use. Covers what was invoiced, route/lane, pricing notes. |

---

### 9. AI Bill Auto-Fill (Vendor Bills)
**File:** `models/documents_ml.py`, `services/bill_scan_service.py`
**Appears on:** Documents app → bills-entry folder → "Process with AI" button; also Vendor Bill form → "AI: Fill from Attachment"

| Function | Description |
|---|---|
| `action_process_with_ml()` | Processes binary documents from bills-entry folder. Extracts vendor, amount, line items, date, tax. Creates draft vendor bill. Moves attachment to bill. Saves pattern to ML knowledge base. |
| `action_scan_folder_only()` | Discovers new files in bills-entry and creates pending import records without processing. |
| `action_process_selected()` | Runs AI extraction on selected pending import records. |

---

### 10. Geotab ELD Integration
**Files:** `models/geotab_sync.py`, `services/geotab_service.py`
**Settings:** Settings → Geotab / ELD

| Cron | Interval | Description |
|---|---|---|
| Vehicle Sync | 1 hour | Syncs device list from Geotab to fleet.vehicle records |
| Telematics Sync | 30 min | Syncs odometer, engine hours, fuel %, DEF %, GPS location |
| Fuel Average Sync | Weekly | Computes avg km/L and L/100km from trips + fuel data |
| Driver Sync | 1 hour | Syncs driver profiles from Geotab to res.partner contacts |
| Driver Log Sync | 1 hour | Syncs duty status logs, driving hours, distance per driver |
| Daily Odometer | Midnight | Snapshots daily odometer + engine hours per vehicle |

**Fuel efficiency sources tried (in order):**
1. Trip.fuelConsumed (CAN-bus direct)
2. Trip.averageFuelEconomy (Geotab computed)
3. DiagnosticEngineTotalFuelUsedId (J1939 counter delta)
4. FuelTransaction records
5. Fuel level sensor integration (level drops × tank capacity)

---

### 11. IFTA Fuel Log
**File:** `models/ifta_fuel_log.py`
**Appears on:** Prema AI menu → IFTA Fuel Log

Auto-created when a fuel vendor bill is processed. Extracts:
- Purchase date, vendor, station address, city, province
- IFTA jurisdiction (auto-detected from postal code, province name, or OCR text)
- Fuel type (Diesel / Gasoline / DEF)
- Litres, US Gallons, price per litre, tax amount, total

Group by Quarter and Jurisdiction for IFTA quarterly reporting.

---

### 12. ML Knowledge Base (Learning System)
**Files:** `models/ml_knowledge.py`, `models/ml_draft.py`, `models/ml_ingestion.py`
**Appears on:** Prema AI menu → Knowledge Base / Pending Review / Data Ingestion

The AI learns from every human decision:

| Origin | Weight | When Created |
|---|---|---|
| `approved` | 2.0 | Human approved AI draft as-is |
| `edited` | 1.5 | Human edited draft before approving |
| `rejected` | 0.5 | Human rejected draft (with feedback note) |
| `manual` | 2.5 | Staff manually saved a lead as example |
| `ingested` | 1.0 | Bulk-ingested from historical records |
| `negotiation` | 1.5 | Agreed WA negotiation rate |
| `vendor_profile` | 3.0 | Built from posted vendor bill patterns |

**Knowledge types:**
- `rate_quote` — freight pricing examples
- `crm_reply` — CRM email drafts
- `wa_reply` — WhatsApp reply examples
- `bill_import` — vendor bill extraction patterns
- `invoice_flag` — invoice anomaly baselines
- `load_tender` — route sheet parsing patterns
- `customer_tag` — contact tagging examples
- `company_document` — Knowledge Center documents

**Data Ingestion** (`action_run()`):
Ingests: sale orders, invoices, vendor bills, customer invoices, WhatsApp conversations, tagged partners, CRM messages, WA negotiations, purchase orders, fleet vehicles, contact profiles.

---

### 13. Business Profile (Singleton)
**File:** `models/business_profile.py`
**Appears on:** Prema AI menu → Business Profile

Single record that stores:
- Company name, overview, services, key differentiators, pricing context
- Tone of voice (Professional / Friendly / Direct / Formal)
- Ideal Customer Profile: target industries, company sizes, seniority levels, job titles, regions
- Non-response threshold (days before contact rotation triggers)

`get_system_prompt()` — builds the master AI system prompt combining company identity, ICP, and Knowledge Center documents. Used by all AI features across the module.

---

### 14. Snov.io Prospect Search Wizard
**File:** `models/snov_search_wizard.py`
**Appears on:** CRM lead → Outreach tab → Search button, or standalone menu

| Function | Description |
|---|---|
| `action_search()` | Searches Snov.io by company domain + optional job title filter. Returns contacts with email confidence scores. |
| `action_create_leads()` | Creates a CRM lead for each selected contact. Also creates res.partner (company + individual) if not existing. |
| `action_import_to_lead()` | Adds selected contacts to an existing lead's Outreach tab. |

Pre-loaded title list for freight/logistics prospecting (Logistics Manager, Director of Supply Chain, VP of Logistics, etc.)

---

### 15. Driver Profile + Geotab Link
**File:** `models/res_partner_extension.py`, `models/contact_geotab_link_wizard.py`
**Appears on:** Contact form → Driver Profile tab (only for Driver-tagged contacts)

Fields added to `res.partner` for drivers:
- Geotab Driver ID link
- Driver license number, status, timezone, home terminal
- Today / This Week: driving hours, on-duty hours, distance km
- Last duty status, last known vehicle, last shift start
- Sync status + error

Wizard `contact.geotab.link.wizard` — select Geotab driver profile, import last 7 days of logs immediately.

---

### 16. POD (Proof of Delivery)
**File:** `models/premafirm_load.py`
**Appears on:** Prema AI → POD Records (via invoice dispatch jobs)

Auto-synced from Fleetbase after job completion. Generates PDF POD report with:
- Company compliance info (CVOR, USDOT, MC, HST)
- Vehicle + driver details
- Load info (sale order, BOL, reefer setpoint)
- Pickup and delivery addresses + times
- Pallet counts + product
- Signature fields

---

## API Keys Required

| Service | System Parameter Key | Where to Get |
|---|---|---|
| OpenAI / Claude | `openai.api_key` | platform.openai.com |
| Snov.io | `snov.client_id` + `snov.client_secret` | app.snov.io → Profile → API |
| MapBox | `mapbox.access_token` | account.mapbox.com |
| Google Maps | `google_maps_api_key` | console.cloud.google.com (Places API) |
| Geotab | Settings → Geotab section | MyGeotab credentials |
| Fleetbase | `fleetbase.api_key` | Fleetbase → Settings → API Keys |

---

## Scheduled Actions (Crons)

| Name | Model | Interval | Active |
|---|---|---|---|
| CRM: AI Follow-up Draft Generator | crm.lead | Daily 07:00 | Yes |
| CRM: Cold Lead Reactivation | crm.lead | Weekly | Yes |
| CRM: Process Bulk Email Queue | premafirm.crm.bulk.email.queue | Every 1 min | Yes |
| ML: Auto-tag Untagged Contacts | res.partner | Daily | Yes |
| ML: Nightly Incremental Ingest | premafirm.ml.ingestion | Daily | Yes |
| ML: Process Ingest Queue | premafirm.ml.ingest.queue | Every 10 min | Yes |
| ML: Purge Response Cache | premafirm.ml.response.cache | Weekly | Yes |
| ML: CRM Contact Rotation Check | premafirm.crm.contact.rotation | Daily | Off (enable manually) |
| Knowledge Center: Index New Documents | premafirm.ml.ingest.queue | Every 1 hour | Yes |
| Geotab: Vehicle Sync | premafirm.geotab.sync | 1 hour | Off (enable in Settings) |
| Geotab: Telematics Sync | premafirm.geotab.sync | 30 min | Off |
| Geotab: Fuel Average Sync | premafirm.geotab.sync | Weekly | Off |
| Geotab: Driver Sync | premafirm.geotab.sync | 1 hour | Off |
| Geotab: Driver Log Sync | premafirm.geotab.sync | 1 hour | Off |
| Geotab: Daily Odometer Sync | premafirm.geotab.sync | Daily midnight | Off |
| Geotab: Monthly Avg km Update | premafirm.geotab.sync | Monthly | Off |
| PremaFirm: Sync Completed Jobs & Generate PODs | premafirm.load | Every 4 hours | Yes |
| Bill Scan: Auto-process bills-entry folder | documents.document | Every 5 min | Off |

---

## CRM Stages Added

| Stage | Purpose |
|---|---|
| Suggestion | Lead created by Snov.io escalation — pending Ahmad's approval before outreach |

---

## Menu Structure

```
Prema AI (root menu)
├── Business Profile
├── Pending Review          ← AI drafts awaiting human decision
├── All Drafts
├── Knowledge Base          ← Full ML learning corpus
├── Data Ingestion          ← Bulk historical data ingestion
└── IFTA Fuel Log

CRM
└── Bulk Email Batches

Accounting → Configuration
└── AI Invoice Products

Accounting → Vendors
└── Bill Scan Imports
```

---

## Email Routing by Partner Tag

The module automatically selects the correct From address based on partner tags:

| Tag | From Address |
|---|---|
| b2b, retail, wholesale | `PremaFirm Inc <sales@premafirm.com>` |
| logistics, carrier, broker, freight forwarder, 3pl | `PremaFirm Logistics <dispatch@logistics.premafirm.com>` |

Configure values in Settings → Email Segment Config (system parameters):
- `premafirm.email_from_inc`
- `premafirm.email_from_logistics`

---

## Known Issues & Fix Log

| Date | File | Fix |
|---|---|---|
| May 13 2026 | `crm_ai_assistant.py` → `action_ai_compose_email` | Added `default_subject` auto-built from lead/company name. Fixed `default_res_id` (singular) for correct chatter threading. Added `default_partner_ids` to pre-fill To field. Added regex strip of accidental "Subject:" lines in AI body. |
| May 13 2026 | `crm_ai_assistant.py` → `_ai_system_prompt` | Added explicit instruction: AI must not write subject lines in email body drafts. |

---

## Developer Notes

### Adding a New AI Feature
1. Add the button to the relevant view XML.
2. Add the method to the model — always `ensure_one()`, always `try/except`.
3. Build the system prompt with PREMAFIRM context from `_ai_system_prompt()` or `profile.get_system_prompt()`.
4. Call `_gpt(env, system, messages, max_tokens)` — handles API key lookup and retry.
5. Return a draft for human review — never auto-send, never auto-create without approval.
6. Log to the correct place: company info → company Log Note, contact info → contact Log Note.

### Module Upgrade
```bash
# SSH into server
ssh root@72.60.115.139
# Upgrade module
/opt/odoo/odoo-bin -c /etc/odoo/odoo.conf -u premafirm_ai_engine --stop-after-init
# Restart service
systemctl restart odoo
```

### Server Path
```
/opt/odoo/custom-addons/premafirm_ai_engine/
```

---

## Contact

**Ahmad Ibrahim**
Owner Operator – PREMAFIRM INC.
Phone: 416-505-3510
Email: ahmad@premafirm.com
Website: https://logistics.premafirm.com
Office: +1-905-916-2468 ext. 102
Address: 7A-994 Westport Cres, Mississauga, ON L5T 1G1, Canada
