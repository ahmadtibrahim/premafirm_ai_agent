# A3 — Recurring-Opportunity Bridge Contract (engine → dispatch)

**Work package:** MP1 E-A3 (master instruction §2.7 recurring-opportunity
management, §17.1-17.2 linkage intent).
**Status:** engine side DONE in this repo (branch `mp1/a3-ongoing-recurring`,
module `premafirm_ai_engine`, 18.0.7.10.0); the dispatch-side companion
commit (authored separately in the dispatch repo) must implement this
contract.

## 1. Module-direction constraint (non-negotiable)

`prema_dispatch` → `prema_logistics_booking` → `premafirm_ai_engine`.
The engine module depends on **no** dispatch module. Therefore:

* the engine model `crm.recurring.opportunity` is the only cross-repo
  record type the bridge needs; it holds **no** logistics references and
  performs **no** logistics calls (it physically cannot);
* everything that touches `logistics.*` models lives in the dispatch repo
  (companion commit), exactly like the existing `crm.lead` extension
  precedent (`prema_logistics_booking/models/crm_lead_rate_confirmation.py`
  extends `crm.lead` from the dispatch side);
* the dispatch repo may extend engine views/records by XMLID: the
  companion inherits `premafirm_ai_engine.view_crm_recurring_opportunity_form`
  etc.

## 2. The engine model — `crm.recurring.opportunity`

Odoo model `crm.recurring.opportunity` (`mail.thread` +
`mail.activity.mixin`), `_rec_name = 'name'`, one open record per
**(lead, expected frequency, partner)**. Exact field names:

| Field | Type | Semantics (authoritative for the bridge) |
|---|---|---|
| `lead_id` | M2o `crm.lead` (required, cascade) | The opportunity/deal. Salesperson/assignment is ONLY read here (`lead_id.user_id`), never stored on this model, never overwritten. |
| `partner_id` | M2o `res.partner`, **related stored** to `lead_id.partner_id`, readonly | Customer company. Dedupe key leg. |
| `name` | Char, computed+stored | `"Customer — Frequency — Kind"`. |
| `kind` | Selection `potential` / `contracted` | **Contracted is only legal with `customer_confirmed = True`** (ORM constraint). `action_confirm_customer()` promotes potential → contracted + stamps date/user. |
| `customer_confirmed` | Boolean | Explicit customer agreement (call/email). Gate for contracted AND for dispatch activation. |
| `customer_confirmation_date` / `customer_confirmation_user_id` | Date / M2o `res.users` | Confirmation evidence (audit). |
| `frequency` | Selection `weekly` / `biweekly` / `monthly` / `irregular` | Expected cadence — dedupe key leg. **`irregular` has no dispatch-side generator** (see §5). |
| `frequency_detail` | Char | Free text, e.g. "every other Tuesday". |
| `preferred_monday` … `preferred_sunday` | 7 Booleans | Preferred weekdays (corridor-style day booleans). No selection = not specified. |
| `preferred_days_display` | Char computed | Display only. |
| `start_date` / `end_date` | Date | Effective start / end of the arrangement. |
| `next_followup_date` | Date | Sales follow-up. Setting it schedules **exactly one** deduplicated "Recurring follow-up" activity on this record; moving it reschedules; clearing closes. Bridge must NOT touch it. |
| `expected_pallets`, `expected_weight_lbs` | Integer / Float | Expected per-shipment quantities (may be 0/empty = unknown). |
| `expected_temperature_mode` | Selection `dry` / `reefer` | |
| `required_temperature_c` | Float | Only meaningful when reefer. |
| `expected_equipment` | Char | Free text (e.g. "Reefer 0°C, Van"). |
| `expected_load_type` | Selection `ltl` / `ftl` (default `ltl`) | |
| `commodity` | Char | |
| `agreement_reference` | Char | **Customer** contract/PO text — forward this verbatim (or empty). |
| `activation_state` | Selection `never_activated` → `awaiting_verification` → `awaiting_activation` → `active`, `paused`, `ended` | Lifecycle. See §3. |
| `activated_at` | Datetime | Stamped when reaching `active` (first time). |
| `intent_rate_confirmation` | Boolean | **§17.1-17.2 intent flag**: "Create Rate Confirmation when activated". Engine side is inert by design (no hook, no cron) — only the bridge consumes it, at activation. |
| `dispatch_agreement_reference` | Char readonly | Anchor written back by the bridge ("CRM-REC-\<id\> …"); informational mirror of §4. |
| `notes` | Text | "Why tracked" context. |

## 3. Activation lifecycle + the event the bridge hooks

Engine-side transitions are **explicit human actions** (buttons on the
form: Start Verification / Mark Ready to Activate / Mark Active / Pause /
End) or the dispatch sync below. Nothing automatic ever moves the state —
in particular **no booking/agreement generation exists on the engine side**.

**The activation event (dispatch side must call this):**

```
crm.recurring.opportunity.prema_sync_activation_from_dispatch(
    agreement_state, agreement_reference=None, reason='')
```

* `agreement_state`: the `logistics.recurring.agreement.state` value —
  mapping: `active → active`, `paused → paused`, `expired → ended`,
  `cancelled → ended`
  (unknown values raise).
* Preconditions: to sync to `active` the opportunity must be
  `kind == 'contracted'` **and** `customer_confirmed == True`, else the
  method raises `UserError`. **The bridge must pre-check this before
  mutating anything** (and surface a clear message) — activation must not
  be attempted for potential/unconfirmed cadences.
* Effects: writes `activation_state`, stamps `activated_at` (first
  `active` only), stores `dispatch_agreement_reference` (first sync only,
  never cleared afterwards), posts an audit chatter note (who + when +
  agreement state). Idempotent: replaying the same state returns `False`
  and adds no note.
* Call site: the bridge's agreement `action_activate()` /
  `action_pause()` / `action_expire()` / `action_cancel()` overrides, after
  the agreement write succeeds, per agreement that carries
  `crm_opportunity_id`. Run with the acting user's env (audit captures who).

## 4. Creating / linking `logistics.recurring.agreement` (anchor)

Verified against
`prema_logistics_booking/models/logistics_recurring_agreement.py`
(READ-ONLY reference, 18.0.13.53.0):

* The agreement already has `agreement_reference = fields.Char("Customer
  Contract / PO")` — **that field is the cross-module anchor**. The
  companion must store the OPPORTUNITY id in it so the engine-side
  back-reference and reverse lookups work without new columns:
  `agreement_reference = "CRM-REC-%d%s" % (opportunity.id,
   " — " + po if (po := opportunity.agreement_reference) else "")`.
  Reverse lookup: `search([('agreement_reference','=like','CRM-REC-<id>%')])`.
* Existing agreement facts the bridge works with (do not duplicate):
  `state` draft/quoted/active/paused/expired/cancelled; `action_activate()`
  requires ≥1 active job with validated values and sets each job's
  `next_shipment_date`; generation is the cron
  `ir_cron_logistics_generate_recurring_bookings` → `_generate_due_bookings()`
  (activation-gated + idempotent via `idempotency_key`); max 10 jobs;
  job `preferred_weekday` is a Selection `"0"`(Mon)…`"5"`(Sat) — **no
  Sunday**, `monthly_week` for monthly jobs; job fields
  `pallets/weight_lbs/load_type/temperature_mode/required_temperature_c/
  commodity/auto_generate` mirror the opportunity's `expected_*` fields.
* Field mapping when creating the agreement + jobs (opportunity →
  agreement/job): `partner_id` → `partner_id`; `agreement_reference` →
  anchor per above; `start_date`/`end_date` → `start_date`/`end_date`
  (`end_date` is REQUIRED on the agreement — when the opportunity has no
  end date, default to start + 365 days and put the fact in the
  agreement's `service_notes`); `expected_*` → the matching job fields
  (only when set); `lead_id.user_id` → `account_manager_id`; cadence:
  **one job per distinct selected preferred weekday** (job
  `preferred_weekday` = dispatch index 0-5), each job carrying the shared
  `frequency` (`monthly_week` default "1"); no day selected → one job on
  Monday (`"0"`).
* Reverse linkage (agreement → CRM opportunity): companion adds
  `crm_opportunity_id = fields.Many2one("crm.recurring.opportunity",
  ondelete="set null", index=True, tracking=True)` on
  `logistics.recurring.agreement` (optionally mirrored on
  `logistics.recurring.job` as related) + a small stat button on the
  agreement form opening the opportunity (engine form by XMLID inherit).
  The engine never needs a hard reverse link — `dispatch_agreement_reference`
  is its informational mirror.
* Sync rules for the companion: on agreement state writes, call §3 only
  for agreements with `crm_opportunity_id`; wrap in try/except only for
  logging, since §3 preconditions are checked before any mutation.
* Activation UX (companion): add the "Create Recurring Agreement" button
  to the opportunity form by inheriting
  `premafirm_ai_engine.view_crm_recurring_opportunity_form`; make it
  visible for `kind == 'contracted'` and `customer_confirmed` and
  `activation_state not in ('ended',)`; when the human activates the
  agreement in the dispatch app the sync event (§3) moves the opportunity
  to `active`.
* **`intent_rate_confirmation` consumption (§17.1-17.2):** when an
  agreement carrying `crm_opportunity_id` with `intent_rate_confirmation`
  reaches `state == 'active'`, the companion must run the dispatch repo's
  canonical customer rate-confirmation flow (the same flow a confirmed
  one-off booking follows — customer rate confirmation / sale-order
  quotation side, see dispatch-side confirm machinery; the companion
  chooses the exact call). The engine guarantees nothing but the flag's
  stability and that it is human-set before activation. If the flag is
  set but the flow fails, log + warn-activity on the agreement — never
  block activation.
* **`irregular` frequency:** the dispatch generator only knows
  weekly/biweekly/monthly. Irregular opportunities stay engine-managed:
  the companion must NOT auto-create a generating agreement for
  `frequency == 'irregular'` (optionally create a manual/tracking
  agreement with `auto_generate` jobs off — companion judgement, but no
  cron-driven booking generation).

## 5. Explicit non-contract (what the bridge must NOT do)

* Do not let the engine generate anything — it cannot and must not be
  made to: no hooks, no cron, no `logistics.*` import may ever appear in
  the engine repo.
* Do not write `crm.recurring.opportunity.partner_id` (related stored) or
  any `res.users`/salesperson field from the bridge.
* Do not schedule/close the opportunity's follow-up activities from
  dispatch code.
* Do not delete engine records — `action_end()`/`prema_sync(...,'ended')`
  are the closing paths (they release the dedupe key and archive the
  intent).

## 6. Companion implementation size target

~150-300 lines of dispatch code across (typically):
`models/logistics_recurring_agreement.py` (crm_opportunity_id, anchor
logic, activation/pause/expire/cancel sync overrides, rate-confirmation
intent hook), `views/logistics_recurring_agreement_views.xml` +
one engine-form inheritance record for the bridge button, security row,
and a `tests/` file stubbing the engine model.

Engine test tag (run before dispatch-side merge, in the engine repo):
`--test-tags /recurring_opportunity`.
