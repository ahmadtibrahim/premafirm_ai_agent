# PremaFirm AI Engine (Odoo 18)

## Dispatch + CRM → Sales → Invoice Intelligence Upgrade

This module upgrades lead dispatch planning, routing, product assignment, sales-order creation, and invoice propagation for PremaFirm workflows.

## 1) Mapbox Integration Details
- Service: `premafirm_ai_engine/services/mapbox_service.py`
- Uses Mapbox Geocoding + Directions APIs.
- Yard origin defaults to: `5585 McAdam Rd, Mississauga ON L4Z 1P1`.
- Computes segment-by-segment travel from yard to first stop, then stop-to-stop.
- Persists segment values into stop-level distance/hours and lead totals.

## 2) Speed Calculation Logic
For each route segment:
- If highway ratio is > 60% (derived from maxspeed annotations): average speed = **95 km/h**.
- Else: average speed = **55 km/h**.
- `drive_time_hours = distance_km / avg_speed`.

Buffers:
- +15 minutes truck warm-up before first movement.
- +10 minutes traffic buffer applied in ETA sequencing.

## 3) Break Scheduling Rules
- First pickup without customer time window defaults to **9:00 AM local**.
- Explicit customer times (for example 8:00 AM) are respected.
- Break rules:
  - After 4 hours drive: 10 minutes
  - After 8 hours drive: 30 minutes
  - After major break: additional 10-minute micro-break in subsequent long segments
- `Leave Yard At` is computed from first pickup time minus first segment travel and startup/buffer offsets.

## 4) Freight Product Selection Matrix
Stop-level field:
- `premafirm.dispatch.stop.freight_product_id`

Selection rules:
- Pickup stop:
  - If total pallets >= inferred truck pallet capacity ⇒ FTL
  - Else ⇒ LTL
- Multi-delivery loads force delivery stops to LTL.
- Country split:
  - US delivery: `FTL USA` / `LTL USA`
  - Non-US delivery: `FTL Canada` / `LTL Canada`

## 5) CRM → SO → Invoice Mapping Table
### CRM Lead → Sale Order
- `partner_id` → `partner_id`
- pickup city (first pickup address city) → `pickup_city`
- delivery city (first delivery address city) → `delivery_city`
- `total_pallets` → `total_pallets`
- `total_weight_lbs` → `total_weight_lbs`
- `total_distance_km` → `total_distance_km`
- `premafirm_po` → `client_order_ref`
- `premafirm_bol` → `premafirm_bol`
- `premafirm_pod` → `premafirm_pod`
- `payment_terms` → `payment_term_id`

### Sale Order → Invoice (`account.move`)
- `premafirm_po` → `ref`
- `premafirm_bol` → `premafirm_bol`
- `premafirm_pod` → `premafirm_pod`
- `load_reference` → `load_reference`
- `client_order_ref` → `payment_reference`

## 6) PO Auto-Detection Logic
PO detection in parsing layer includes:
- `PO`
- `Purchase Order`
- pattern capture for PO number tokens

Detected PO is saved on lead as `premafirm_po` and mapped to SO `client_order_ref`.

## 7) USA/Canada Accounting Auto-Switch
On SO creation:
- US customer country:
  - attempts US fiscal position
  - attempts US sales journal
  - sets currency to USD
- Otherwise:
  - sets currency to CAD

Invoice creation defaults `currency_id` to company currency when absent to avoid secondary currency forcing.

## 8) POD Generation Flow
- QWeb report file: `views/report_premafirm_pod.xml`
- SO button: **Generate POD**
- Report includes:
  - Sales Order number
  - Pickup
  - Delivery
  - Driver
  - Signature placeholder

## 9) Packaging Fields Removed Intentionally
From Sale Order lines (form + list):
- `packaging_id`
- `product_packaging_qty`

These are hidden to keep freight quoting UI focused on dispatch details.

## 10) Known Dependencies
- `sale_management`
- `account`
- `fleet`
- `hr`
- `calendar`
- `mail`

## Testing Checklist
- Multi stop FTL Canada
- Multi stop LTL Canada
- USA FTL
- USA LTL
- PO via PDF
- PO via email body
- No time window → default 9AM
- Custom time window 8AM
- Overlapping schedule detection
- Invoice creation with attachments
- Currency switching
- Secondary currency error resolved
