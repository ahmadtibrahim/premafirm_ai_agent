# PremaFirm AI Engine (Odoo 18)

## Dispatch + CRM → Sales → Invoice Intelligence Upgrade

This module extends dispatch planning and downstream sales/accounting automation for PremaFirm.

## 1) Mapbox integration (driving-traffic)
- Service: `premafirm_ai_engine/services/mapbox_service.py`.
- Uses Mapbox Geocoding + Directions API with `driving-traffic` profile.
- Route chain is computed as:
  - `fleet.vehicle.home_location` → stop 1 → stop 2 → ... → last stop → `home_location`.
- Supports geocoding from full addresses and city/province strings.
- For each leg, stop records get:
  - `distance_km`
  - `drive_hours`
  - `map_url`

## 2) HOS and ETA logic
- Implemented in `premafirm_ai_engine/services/crm_dispatch_service.py`.
- Speed rule:
  - >60% highway: 90 km/h
  - otherwise: 45 km/h
- Breaks included directly into `drive_hours`:
  - 10–15 min after ~4h driving (implemented as 15 min)
  - 45 min after ~8h driving
  - +10 min micro-break after 3–4h post major break
- Departure (`crm.lead.departure_time`) is computed from first pickup schedule minus:
  - first leg travel,
  - 15 min warm-up,
  - 10 min traffic buffer.
- ETA (`premafirm.dispatch.stop.estimated_arrival`) is sequenced from previous time + effective drive time.
- Aggregates are persisted on lead:
  - `total_distance_km`
  - `total_drive_hours`

## 3) Default scheduling rules
- If first pickup has no window and no explicit schedule, defaults to 9:00 AM local.
- Pickup/delivery windows are respected when provided.
- Delivery ETA rolls into same day when possible; otherwise naturally pushes to next day based on cumulative travel + HOS break additions.

## 4) Stop-level service product selection
- Dispatch stop now has explicit service product:
  - `premafirm.dispatch.stop.product_id`
- Backward compatibility kept through related alias:
  - `freight_product_id` → related to `product_id`
- CRM line label is now **Service** in stop table.
- Product selection considers:
  - country (Canada vs USA),
  - stop type / multi-delivery behavior,
  - total pallets vs inferred capacity,
  - reefer vs dry service type.

## 5) CRM → Sales Order creation and mapping
- CRM button: **Create Sales Order**.
- Behavior:
  - PO present (`po_number`) ⇒ confirms SO (`state='sale'`)
  - no PO ⇒ stays draft quotation.
- Lead→SO mapping includes:
  - partner, PO/BOL/POD,
  - pallets/weight/distance totals,
  - pickup/delivery city,
  - payment terms.
- One SO line per stop:
  - `product_id` from stop service,
  - `name` from stop address,
  - `product_uom_qty` from stop pallets,
  - `price_unit` proportional share of total rate,
  - `scheduled_date` from stop schedule,
  - line discount from lead discount %.
- Packaging columns are hidden in SO line list/form.

## 6) Discount system
- If user sets `crm.lead.final_rate`, module computes:
  - `discount_amount = suggested_rate - final_rate`
  - `discount_percent = discount_amount / suggested_rate * 100`
- Discount percent is propagated to each SO line (`sale.order.line.discount`).

## 7) Calendar scheduling / driver booking
- When vehicle is assigned and routing completed, module creates/updates calendar event:
  - `name = Load #<lead.id>`
  - start = `departure_time`
  - stop = last stop ETA
  - attendees = assigned driver partner
  - linked to `crm.lead`
- Overlap check blocks booking when driver already has overlapping event.

## 8) POD document generation
- On SO confirmation, POD PDF is generated and attached to `sale.order`.
- POD report includes:
  - SO number,
  - PO/BOL/POD,
  - stop rows with scheduled/ETA,
  - driver and vehicle.

## 9) Multi-country accounting logic
- For US customers:
  - prefers USA company,
  - USA sales journal,
  - USD currency.
- For non-US customers:
  - prefers Canada company,
  - Canada sales journal,
  - CAD currency.
- Invoice prep mirrors the same company/journal selection intent.

## 10) Key dependency modules
- `crm`
- `sale_management`
- `account`
- `mail`
- `fleet`
- `hr`
- `calendar`

## 11) Odoo 18 view type compatibility note
- Odoo 18 no longer accepts `<tree>` as a view root tag for list views.
- Use `<list>...</list>` instead.
- For this module, `premafirm_ai_engine/views/premafirm_load_view.xml` was updated from tree to list view syntax.
- Team rule: **do not introduce new `<tree>` views**; always define list views with `<list>` in Odoo 18.

## Odoo 18 Report Inheritance Rule

When inheriting QWeb reports:

DO NOT use class-based XPath expressions such as:

    //div[@class='row mt32 mb32']

Reason:
- CSS classes change between Odoo versions.
- Report structure is not guaranteed stable.
- Causes registry load failures.
- Breaks module upgrades.

Instead, always target stable IDs:

Examples:

    //div[@id='informations']
    //div[@id='total']
    //div[@id='payment_term']

ID-based targeting is stable across Odoo 18 updates.

If unsure:
1. Open Odoo shell
2. Inspect parent view:
       env.ref('sale.report_saleorder_document').arch_db
3. Verify real structure before writing XPath.

This rule is mandatory for all future report inheritance work.
