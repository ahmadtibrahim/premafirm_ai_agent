# Studio CRM Rule Disable Runbook — 2026 cutover (issue #13 / §2.6)

**When to run this:** AFTER this module version (18.0.7.7.0+) is deployed
and the post-deploy smoke test has passed. The versioned "code twins" in
`data/crm_automation_fixes.xml` ship ACTIVE, so from the moment the
upgrade finishes they fire on the same events as the Studio rules below —
the disable steps close that double-fire window and remove the unsafe
anonymous rules.

**What NOT to do:** never delete the Studio rows (rollback = re-enable).
Never disable the module's own `premafirm_ai_engine.automation_*` twins —
they are the replacements. Rule numbers below are the audit/ops labels
from issue #13 and can drift between databases; identify every row by its
behavior (name, trigger, action code), never by number or id alone. The
server-action ids (`sa 1218 / sa 1513 / sa 1518`) are DB hints only.

---

## 1. The five rules, identified by behavior

Open **Settings › Technical › Automated Actions** (model `base.automation`;
enable Developer Mode first if the menu is hidden) and locate the rows.

| # | Row you are looking for | Trigger / model | Behavior fingerprint (action code) | Module replacement | Danger if left active |
|---|---|---|---|---|---|
| **60** | ~"CRM: Replied" / "Incoming reply → ENGAGED" | Incoming Message (`on_message_received`) on `crm.lead` | Moves the lead to ENGAGED / REPLIED on a `normal_reply` **regardless of current stage** | `automation_engaged_on_normal_reply` → `prema_process_normal_reply()` (early stages only) | **S1.** Replies on QUOTE SENT / NEGOTIATION / post-sale leads regress them to ENGAGED (B-4); also double-funnels activities |
| **61** | ~"Notify Ahmad of new Sales team leads" | On creation (`on_create`) on `crm.lead` | Notifies a **hard-coded partner id 3** (action code contains `partner_ids=[3]` or a literal partner id) | `automation_new_sales_lead_notify` → `prema_process_new_sales_lead()` (param-driven `crm.new_lead.*`) | **S1.** Duplicate notifications after deploy; breaks silently if partner 3 ever changes (B-3) |
| **63** | ~"Callback Request - tag, notify, note" | On creation (`on_create`) on `crm.lead`, name filter "Callback Request" / "Quote Request" | Calls the callback handler and posts a **banner note without an internal subtype** (may email customer followers); no dedupe | `automation_website_callback` → `prema_process_website_callback()` (all notes `mt_note`, dedupe-idempotent) | **S1.** Customer followers can be emailed by the banner; duplicates on reprocessing (B-6) |
| **64** | "CRM: NEW / UNCONTACTED → OUTREACH SENT (contact details complete)" | On update (`on_write`) on `crm.lead`, **no watched field**; action `sa 1518` writes OUTREACH SENT | Any write (note, chatter, edit) to a matching NEW lead advances the stage | Code hook `_message_post_after_hook` (genuine outbound only) — row deactivated by migration 18.0.7.7.0 | **S1.** Any edit silently advances NEW leads (B-2). **Migration already turned this row OFF — verify it, do not re-enable** |
| **51** | Broken nightly rule referencing removed field `x_waiting_reply` | Time/other trigger (nightly); action `sa 1218` | Runs code on a removed field → `safe_eval` NameError in the logs | none (no twin — nothing to replace, it is dead) | Nightly error noise; any partial effect is unverified (B-5) |

> Note: the module's own `automation_new_to_outreach` XML record in
> `data/crm_workflow_automations.xml` IS rule 64's row (xmlid
> `premafirm_ai_engine.automation_new_to_outreach`). Migration
> `migrations/18.0.7.7.0/post-migrate.py` set it inactive on upgrade; the
> data file ships `active="False"` for fresh installs. **Verify** the live
> row shows Active unchecked — if it is somehow still active, uncheck it
> now (step 4).

## 2. Disable procedure

For each rule to disable (60, 63, 61, 51):

1. In **Settings › Technical › Automated Actions**, open the row.
2. Uncheck **Active**.
3. Click **Save**.

**Recommended order (highest harm first — the code twins are already
live, so 60/61/63 are double-firing from the moment the upgrade ends):**

| Step | Rule | Why this order |
|---|---|---|
| 1 | **60** | Worst live harm: stage regression on real replies (incl. post-sale downgrades) + double activity funnels |
| 2 | **63** | Unsubtyped banner can email customer followers; duplicate tag/notes/activity per callback |
| 3 | **61** | Duplicate "new lead" internal notifications + hard-coded partner 3 |
| 4 | **51** | Dead rule — stop the nightly NameError |
| 5 | **64** | Verify only (migration disabled it); uncheck if found active |

Disabling all five in one pass right after smoke is fine; the order only
matters if you must leave the cutover half-done — in that case leave 60
disabled first and never leave the building with 60 still active.

## 3. Post-checks

1. Re-open the Automated Actions list — all five rows show **Inactive**.
2. Re-run the deploy smoke test (create a callback lead; post an inbound
   normal reply on an early-stage lead and on a terminal-stage lead — the
   early one may move to ENGAGED / REPLIED, the terminal one must NOT
   move and must show Needs Attention + one "Respond to customer"
   activity).
3. Optional: confirm the log no longer shows the nightly `x_waiting_reply`
   NameError (rule 51) after the next cron pass.

## 4. What breaks if a step is skipped

* **Rule 60 left active:** replies on NEGOTIATION / QUOTE SENT / post-sale
  (WON, ONBOARDING, …) leads regress the stage to ENGAGED — the exact
  downgrade the §2.1 guard forbids; every reply also double-schedules
  activities until you disable it.
* **Rule 63 left active:** callback leads get duplicate tag/notes/activity
  (the twin's dedupe cannot see the Studio row's side effects) and the
  unsubtyped banner may email the customer.
* **Rule 61 left active:** every new Sales-team lead double-notifies
  (Studio row to partner 3 + twin to the configured partner); hard-coded
  partner 3 is unconfigurable.
* **Rule 51 left active:** nightly `safe_eval` NameError persists (noise,
  S2).
* **Rule 64 left active:** any ordinary write to a matching NEW lead
  silently advances it to OUTREACH SENT again (S1).

## 5. Rollback

Re-enable the Studio rows: **Settings › Technical › Automated Actions ›
open the row › check Active › Save** (rows are never deleted, so nothing
is lost). The module-side migration flip for rule 64 is one-way by design
(`noupdate="1"` data + migration) — if you need rule 64's behavior back,
re-enable the row manually. To roll the code back entirely, redeploy the
previous module version; the Studio rules are then your only CRM
automation again.

## 6. Configuration parameters delivered with this cutover

Read/write via **Settings › Technical › Parameters** (model
`ir.config_parameter`):

| Key | Default | Meaning |
|---|---|---|
| `crm.bulk_email.daily_limit` | `0` | **B-11.** Daily send budget for the "CRM: Process Bulk Email Queue" cron (runs every minute). `0` = unlimited. Counts items the script actually hands to SMTP per UTC calendar day (the cron stamps naive UTC). When reached, the cron skips until the next UTC day — queued items simply wait. Set to your provider's daily cap minus margin (e.g. `2000`). |
| `crm.new_lead.notify_partner_id` | admin's partner | Internal partner notified once per new Sales-team lead (replaces Studio 61's hard-coded partner 3). |
| `crm.new_lead.default_salesperson_id` | admin user | Default assignee for new Sales-team leads — applied **only when the lead is still unassigned**; an existing salesperson is never overwritten. |
| `crm.followup.send_mode` | `draft` | Follow-Up Service mode: `draft` / `approval` / `auto`. The B-10 stage guard excludes LOST / PAUSED / ON HOLD and won records from reactivation in every mode. |
