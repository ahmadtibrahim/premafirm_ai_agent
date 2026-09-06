# E-A2 — Cross-module contract: engine estimate-draft services and the
# dispatch-side companion (MP1 Quotation Program)

Two repositories share one Odoo 18 database:

* **engine** (`premafirm_ai_engine`) — CRM/sales/invoice heavy; ALL AI runs
  here through `services/deepseek_utils.py`.
* **prema_dispatch** repo (`prema_dispatch`, `prema_logistics_booking`,
  `prema_dispatch_inbox`) — canonical dispatch pricing, saved locations, and
  the `logistics.custom.quote` "Customer Rate Confirmation" (B1 lifecycle,
  `find_or_create_draft_for_lead`).

Dependency direction is strict: `prema_logistics_booking` depends on
`prema_dispatch`, and `prema_dispatch` depends on `premafirm_ai_engine`.
**Engine code therefore never imports, calls, or depends on any
dispatch/logistics model** (that would be a module cycle). All cross-module
work happens through the contracts below, driven from the dispatch side.

Everything engine-side described here ships in this module
(`premafirm_ai_engine` 18.0.7.10.0) and is test-covered in `tests/`.

---

## 1. The two deliberate staff actions (dispatch-side duty)

The dispatch-side companion adds two buttons on `crm.lead`
(documented here; implementation ships in the prema_dispatch repo, out of
scope of the engine commit):

1. **Prepare Preliminary Estimate Reply**
   → calls `premafirm.lead.estimate.reply.prepare_from_lead(...)` (contract §4)
   and opens the returned draft form for staff editing.
2. **Create Draft Rate Confirmation**
   → calls `logistics.custom.quote.find_or_create_draft_for_lead(lead_id,
   idempotency_key=...)` (existing B1 API). Exactly ONE discoverable, editable
   DRAFT rate confirmation per lead; calling again returns the same draft
   (idempotency-keyed), never a duplicate and never an auto-send.

Both actions gate the older generative-AI Rate Quote path; staff trigger
them deliberately — nothing runs on lead creation and nothing is ever
auto-sent. Send remains exclusively `logistics.custom.quote.action_send_rc`.

## 2. Shipment-fact supersession service (engine)

`odoo.addons.premafirm_ai_engine.services.lead_fact_service`

* `LeadFactService(env).collect_documents(lead)` → ordered customer
  documents `[{kind, source, at, text}]`: the lead description first, then
  inbound customer emails oldest→newest. **Staff-authored messages are
  excluded** (`message_type == 'email'`, author without linked user) — same
  rule the dispatch lead bridge applies in `_dispatch_rate_source_text`.
* `LeadFactService(env).extract_effective_facts(lead, extractor=None,
  docs=None)` → runs the structured extractor per document, then per-field
  supersession:
  `{"effective": {field: fact}, "superseded": [fact, ...], "rows": [...],
  "docs": [...], "warnings": [...]}`
* `resolve_effective_facts(candidates)` — pure function. Rules:
  * per field, the **newest** customer-provided value wins (`at` timestamp);
  * equal timestamps: `inbound_email` > `attachment` > `lead_description`;
  * empty/unknown values ("tbd", "unknown", "n/a", …) never supersede a
    concrete value;
  * every surviving fact keeps provenance: `source` (which document),
    `at` (when), `kind`, `confidence`.

Lead-1041 semantics this guarantees: an older "pickup 09:00–10:00, deliver
by 13:00" in the description is superseded by a newer customer email stating
"pickup 10:30–11:30 a.m., delivery before 4:00 p.m." — effective facts read
10:30/11:30/16:00 and remain visibly sourced.

## 3. Structured extraction service (engine, §5 generalization)

`odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service`

* `ShipmentFactExtractionService(env).extract_from_text(text, source_label,
  kind, at)` → DeepSeek JSON extraction over a **shared shipment-fact
  vocabulary** (`VOCAB`: reference, commodity, equipment,
  temperature_mode/setpoint, package_type, pallets, cases, pieces,
  weight_lbs, dimensions, pickup_date/earliest/latest, delivery_date,
  delivery_deadline, service_minutes, origin/destination
  address/city/postal_code, stops, accessorials, contacts, instructions,
  document_number).
* `extract_from_attachment(b64, mimetype, filename, ...)` and
  `extract_from_attachments(records)` — FILES **and pasted text**, routed
  through the shared zero-cost document extractor (PDF text layer → OCR,
  image OCR, Excel, CSV, plain text).
* Row shape (every value sourced):

      {field, value, source, kind, at, confidence, conflict}

* `conflict=True` where two rows state different values for one field —
  **surfaced for review, never auto-resolved** (LLM prompt instructs one row
  per stated value when the source contradicts itself).
* Off-vocabulary and empty values are dropped with warnings — never facts.
* **Hard guarantees: no booking/quote creation, no send, no write of any
  kind** (test-covered).

## 4. Preliminary estimate draft model (engine)

`premafirm.lead.estimate.reply` — one discoverable editable DRAFT per staff
trigger. `models/premafirm_estimate_reply.py`.

```python
@api.model
def prepare_from_lead(self, lead, price_amount, currency=None,
                      price_reference="", extractor=None)
    -> premafirm.lead.estimate.reply   # created record
```

* **`price_amount` is required** (UserError otherwise) and must come from the
  dispatch pricing authority — `BookingOrchestrationService` /
  `PricingService` — NEVER from the engine or the LLM.
* The LLM writes prose only; the price line and the concise NON-BINDING
  disclaimer are injected programmatically afterwards. The generated draft is
  never sent by the engine (no send API on the model, no `mail.mail`).
* `facts_snapshot` (Json) carries `{effective, superseded, sources,
  warnings}` with provenance for the reviewer; `price_reference` records
  where the price came from.
* Discovery: the draft is linked on the lead via
  `crm.lead.x_estimate_reply_ids`, with
  `crm.lead.action_open_estimate_replies()` window action; engine views
  (`views/premafirm_estimate_reply_views.xml`) are list/form only.
* Acceptance/revision stay **flags only** (the lead's `x_needs_attention`
  etc.) — the dispatch companion sets them; preparing/editing a draft never
  moves pipeline state and never writes to the customer.

## 5. Price-source rule (binding)

Price text in any staff-approved draft originates ONLY from the dispatch
pricing services (`logistics.booking.orchestration` /
`logistics.pricing.rule` evaluation), passed into `prepare_from_lead` as a
number plus provenance string. The engine (and any LLM prompt it runs)
never invents, extrapolates, or echoes a price.

## 6. Location resolution (dispatch-side duty)

Origin/destination resolution reuses the existing dispatch
saved-location / city-only rules (engine has no location logic of its own —
see prema_dispatch commit 00b9a58). The companion maps extraction fields
(`origin_city`, `origin_postal_code`, `origin_address`, …) onto those
resolvers before drafting the RC.

## 7. Companion checklist (dispatch-side commit, not in this engine commit)

- [ ] Two lead buttons calling the APIs of §1; nothing auto-triggered.
- [ ] Pricing through dispatch services only; hand `price_amount` +
      `price_reference` to `prepare_from_lead`.
- [ ] Draft RC via `find_or_create_draft_for_lead` (idempotency-keyed);
      keep `action_send_rc` as the only sender.
- [ ] Map extraction rows → `logistics.custom.quote` fields; reuse
      saved-location/city-only resolution.
- [ ] Flag-only acceptance/revision (`x_needs_attention` et al.).
- [ ] Note: the staged B1 branch (`mp1-dispatch`) currently cannot upgrade in
      a scratch DB — its `logistics_custom_quote_views.xml` still contains an
      Odoo-18-invalid `attrs=` (line ~118, `conversion_override_reason`) and
      a forward XML reference to the Revise-wizard action. Fix dispatch-side
      before staging B1 for UAT.
