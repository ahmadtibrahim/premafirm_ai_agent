# PremaFirm AI Engine (Odoo 18)

PremaFirm AI Engine is an Odoo 18 custom module that adds:
- AI-based extraction of dispatch stops (pickup/delivery) from broker/customer emails on CRM leads
- Mapbox routing per leg (distance + drive time)
- Rule-based pricing with accessorials (liftgate, inside delivery, detention requested)
- Draft quote generation for the "Send message" composer (manual review/edit required)
- Pricing history learning: capture final manually edited quote rates at send-time for future suggestions

This module is designed for a HUMAN-IN-THE-LOOP workflow:
- AI generates suggestions
- Dispatcher edits the final quote price manually
- Dispatcher clicks Send (no auto-send by the AI)

## Odoo Version Support
- Target: Odoo 18.x (Community/Enterprise)
- View XML: uses `<list>` for list views (Odoo 18 list view root element is `<list>`).
- Conditional UI logic (if used later) should follow Odoo 18 style (e.g., `invisible="python_expression"`).

## Features Overview

### CRM Lead Enhancements
Adds to `crm.lead`:
- Stops: `dispatch_stop_ids` (one2many of `premafirm.dispatch.stop`)
- Totals: `total_distance_km`, `total_drive_hours`, `total_pallets`, `total_weight_lbs`
- Accessorial flags: `inside_delivery`, `liftgate`, `detention_requested`
- Vehicle: `assigned_vehicle_id`
- Pricing outputs: `estimated_cost`, `suggested_rate`, `ai_recommendation`

### Stop Model
Model: `premafirm.dispatch.stop`
Stores each pickup/delivery address plus pallets, weight, and routed distance/time.

### Pricing History Learning
Model: `premafirm.pricing.history`
Captures:
- `customer_id`
- `pickup_city` / `delivery_city`
- `distance_km`, `pallets`, `weight`
- `final_price` (taken from the final edited compose body at send-time)

## Configuration (System Parameters)
Set these parameters in **Settings → Technical → Parameters → System Parameters**:
- `openai.api_key`
- `mapbox_api_key`

## Usage
1) Open CRM → Leads
2) Open a lead created from an inbound email (or containing message content)
3) Click **AI Calculate**
4) Click **Send message**
5) Edit price manually and click **Send**

When sent, the final edited `$` price is captured into `premafirm.pricing.history`.

## Troubleshooting
- Error: `OpenAI API key missing.` → set `openai.api_key`
- Error: `Mapbox API key missing.` → set `mapbox_api_key`
- Error: `AttributeError` on `mail.message.body_plaintext`
  - This environment does not expose `body_plaintext`.
  - Use HTML-to-text conversion from `mail.message.body`.

## Developer Notes (Odoo 18 specifics)
- `mail.message` stores HTML in `body`; do not assume a full plaintext field exists.
- `mail.compose.message` targets records via `model + res_ids`; avoid `default_res_id`.
- Skip pricing history logging on internal notes (`wizard.subtype_is_log`).
