# PREMAFIRM AI DISPATCH ENGINE
**Odoo 18 Enterprise – CRM + Fleet + Accounting + AI Dispatch Automation**

This module extends Odoo 18 Enterprise to automate freight-intake, dispatch planning, routing, and quote recommendations for Ontario-focused LTL/FTL operations.

---

## System Overview

PremaFirm AI Dispatch Engine provides:

- AI email extraction (email body + PDF attachments)
- Multi-stop dispatch table and route sequencing
- Mapbox geocoding + distance/ETA calculation
- Ontario LTL/FTL pricing engine
- HOS-compliant scheduling checks
- Automatic suggested-rate calculation
- CRM quote/reply generation support

---

## Current Verified Models & Fields

### `crm.lead`
Core fields available:

- `name`, `description`, `email_from`, `phone`, `mobile`
- `partner_id`, `contact_name`, `partner_name`
- `street`, `city`, `state_id`, `country_id`, `zip`
- `probability`, `stage_id`, `user_id`, `team_id`, `company_id`
- `message_ids`, `message_attachment_count`, `message_has_error`, `message_needaction`, `has_message`
- `rating_ids`, `website_message_ids`
- `create_uid`, `create_date`, `write_uid`, `write_date`
- `expected_revenue` (monetary)
- `days_exceeding_closing` (float)

### `fleet.vehicle`
Core fields:

- `name`, `license_plate`, `location`, `driver_id`, `driver_employee_id`
- `company_id`, `odometer`, `fuel_type`, `vehicle_type`, `model_id`, `vin_sn`

Custom Studio fields:

- `x_studio_front_axle_rating_lbs`
- `x_studio_rear_axle_rating_lbs`
- `x_studio_gvwr_lbs`
- `x_studio_payload_limit_lbs`
- `x_studio_max_pallets_1`
- `x_studio_fuel_tank_capacity_gal`
- `x_studio_unit_number`

### `mail.message`

- `subject`, `body` (HTML), `email_from`
- `attachment_ids`, `model`, `res_id`, `message_type`, `date`

### `ir.attachment`

- `name`, `datas` (Base64), `mimetype`, `file_size`, `res_model`, `res_id`

---

## New Model to Create

### `premafirm.dispatch.stop`

Required structure:

- `lead_id` (many2one → `crm.lead`, required, `ondelete="cascade"`)
- `sequence` (integer)
- `stop_type` (selection: `pickup`, `delivery`)
- `address` (char, required)
- `latitude` (float)
- `longitude` (float)
- `pallets` (integer)
- `weight_lbs` (float)
- `service_type` (selection: `dry`, `reefer`)
- `pickup_datetime_est` (datetime)
- `delivery_datetime_est` (datetime)
- `time_window_start` (datetime)
- `time_window_end` (datetime)
- `distance_km_from_prev` (float)
- `drive_hours_from_prev` (float)
- `notes` (text)

### Totals to store on `crm.lead`

- `x_total_distance_km` (float)
- `x_total_drive_hours` (float)
- `x_total_pallets` (integer)
- `x_total_weight_lbs` (float)
- `x_estimated_operating_cost` (float)
- `x_suggested_rate` (float)
- `x_ai_recommendation` (text)

---

## Products (from attachment)

Existing service products:

- Inside Delivery
- LTL - Freight Service - USA
- FTL - Freight Service - USA
- LTL Freight Service - Canada
- FTL Freight Service - Canada
- Freight Service
- Detention - CAN
- Liftgate
- Commercial Auto Insurance
- Service on Timesheets
- Daily Backup
- MV Core Smart Chair

Taxes:

- `13% HST`
- `0% Int` (international zero rated)

Usage guidance:

- LTL Canada → default for Ontario
- FTL Canada → full truck
- USA services → cross-border loads
- Inside Delivery / Liftgate / Detention → surcharge lines

---

## Chart of Accounts Mapping

Expected revenue accounts:

- Freight Revenue - Canada
- Freight Revenue - USA
- Service Revenue
- Other Income

Common expense accounts:

- Fuel Expense
- Insurance Expense
- Maintenance
- Office Expense
- Bank Fees
- Software Expense
- Depreciation
- COGS (if used)

Liabilities:

- HST Payable
- Loans Payable
- Accounts Payable

Assets:

- Truck Asset
- Accumulated Depreciation
- Bank
- Accounts Receivable

Posting rule:

- Main freight pricing should post to **Freight Revenue - Canada** or **Freight Revenue - USA**
- Surcharges default to same revenue bucket unless a dedicated mapping is added

---

## Dispatch Rules File

File: `premafirm_ai_engine/data/dispatch_rules.json`

Contains configuration for:

- `base_rate_per_km`
- `fuel_cost_per_km`
- `daily_fixed_cost`
- `target_net_profit`
- surcharge rules
- congestion multiplier
- HOS limits
- minimum load price

Used by:

- AI pricing engine
- ETA/scheduling engine

---

## Integrations

### Mapbox
System parameter:

- `mapbox_api_key`

Used for:

- Address geocoding (address → lat/lon)
- Route calculation
- Distance (km)
- Drive duration (hours)

### OpenAI
System parameter:

- `openai_api_key`

Used for:

- Email body parsing
- PDF extraction
- Multi-load detection
- Service type detection
- Pallet/weight extraction
- Suggested reply generation

---

## How It Works (AI Workflow)

1. Customer/broker email is received.
2. Odoo creates or updates CRM lead.
3. User clicks AI action button on lead.
4. System reads:
   - `mail.message.body`
   - `ir.attachment.datas` (if present)
5. System sends normalized text to OpenAI extraction prompt.
6. AI returns structured JSON load data.
7. System then:
   - creates `premafirm.dispatch.stop` records
   - calls Mapbox for route + ETA
   - applies HOS logic
   - applies pricing rules from `dispatch_rules.json`
   - writes totals/recommendation back to `crm.lead`
8. User reviews recommendation and suggested rate.
9. User sends response/quote.

---

## HOS Logic

Current operational rules:

- 13h max driving per day
- 15min break after 4h
- 30min break after 8h
- Traffic multiplier: 1.18
- Overnight split if limits exceeded
- Respect customer time windows

---

## Ontario LTL Pricing Engine (Baseline)

- Dry base rate: **2.25/km**
- Reefer base rate: **2.55/km**
- Fuel cost: **0.75/km**
- Daily overhead: **155**
- Target net: **400/day**
- Minimum load floor: **450**

Surcharges:

- Extra stops: **+75 each**
- Liftgate: **+85**
- Inside delivery: **+125**
- Detention: **+75/hour**

---

## UI Design Target

CRM → **Load Info** tab

Stops table:

- Sequence | Type | Address | Pallets | Weight | ETA | Distance

Summary fields below table:

- Total KM
- Total Drive Hours
- Total Pallets
- Total Weight
- Estimated Cost
- Suggested Rate
- AI Recommendation

---

## TODO (Implementation Roadmap)

### Phase A — Data Model

- [ ] Create `premafirm.dispatch.stop` model and security access.
- [ ] Add one2many relation from `crm.lead` to stops.
- [ ] Add total fields to `crm.lead` (`x_total_*`, `x_suggested_rate`, recommendation).

### Phase B — AI Intake

- [ ] Read latest lead email message body reliably.
- [ ] Extract and decode supported attachments (PDF/text).
- [ ] Implement OpenAI prompt + schema validation for structured output.

### Phase C — Routing + ETA

- [ ] Geocode each stop via Mapbox.
- [ ] Build route legs and compute per-leg distance/hours.
- [ ] Save totals and per-stop travel metrics.

### Phase D — Pricing + HOS

- [ ] Load and validate `dispatch_rules.json`.
- [ ] Apply Ontario LTL/FTL pricing rules + surcharges.
- [ ] Enforce HOS/break constraints and overnight logic.

### Phase E — CRM UX

- [ ] Add “Load Info” tab in CRM lead form.
- [ ] Add editable stops grid and totals cards.
- [ ] Add “AI Parse & Price” action and status/error messages.

### Phase F — Commercial Flow

- [ ] Map products/taxes to quote lines.
- [ ] Set revenue account mapping (Canada/USA).
- [ ] Generate draft response using suggested rate.

### Phase G — QA / Hardening

- [ ] Unit tests: parser, pricing, HOS edge cases.
- [ ] Integration tests: lead → stop creation → totals.
- [ ] Logging and retry behavior for external APIs.

---

## Future Expansion

- Fuel index integration
- Toronto congestion dynamic pricing
- Customer tier pricing
- Backhaul discount logic
- Automated invoice creation
- Fleet capacity optimization

---

## Compatibility

Designed for:

- Odoo 18 Enterprise
- CRM
- Fleet
- Accounting
- Mail
- Product
- Odoo Studio compatibility

---

## Notes

- This README is the functional blueprint for the current module direction.
- Keep business rules in `dispatch_rules.json` wherever possible to avoid hardcoding.
