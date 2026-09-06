"""Issue 13 — version-controlled replacements for the Studio CRM rules.

Five Studio/base_automation rules ran production CRM behavior as
anonymous records. This module ships the required behavior as code
(handlers) + stable XML records (data/crm_automation_fixes.xml), so no
operationally-critical logic depends on un-versioned Studio rules:

* Studio "CRM: Replied"            → prema_process_normal_reply()
  (automation replacement fires on_message_received, prefiltered to
  ``last_inbound_classification == 'normal_reply'`` — genuine inbound
  external customer mail routed to the lead).  Unlike the Studio rule,
  ONLY early-stage leads (NEW / UNCONTACTED, OUTREACH SENT) move to
  ENGAGED / REPLIED — QUOTE SENT, NEGOTIATION, ONBOARDING, PAUSED, LOST
  and WON records are never moved backwards.  Terminal / post-sale
  stages (WON / ACTIVE CUSTOMER, APPROVED, WAITING FOR LOADS, ONBOARDING
  — §2.1 guard, see ``_TERMINAL_STAGES``) NEVER move either: a genuine
  reply only raises the Needs Attention flag and guarantees the one
  deduplicated "Respond to customer" activity (the flag-and-activity
  branch — no downgrade, ever).  The waiting-response flag and the one
  "Respond to customer" activity come from the PHASE 14 funnel
  (``_on_meaningful_reply``), which is idempotent by design.
* Studio "Notify Ahmad of new Sales team leads" → prema_process_new_sales_lead()
  Notifies the CONFIGURED internal owner (ir.config_parameter
  ``crm.new_lead.notify_partner_id``) once per new Sales-team lead and
  assigns the configured default salesperson
  (``crm.new_lead.default_salesperson_id``) only while the lead is
  unassigned.  No hard-coded partner id — the fallback is the Odoo
  administrator (``base.user_admin``) resolved by XMLID, never a raw id.
* Studio "Callback Request - tag, notify, note" → the automation twin
  fires only when a lead is CREATED with the website names
  ('Callback Request' / 'Quote Request'); the handler in
  crm_lead_extension.py gates the tag/note/activity/notify extras to
  genuine callback submissions and is dedupe-idempotent.
* Rule 4 "…→ OUTREACH SENT (contact details complete)" is DISABLED —
  contact completeness must never mean outreach was sent.  The stage now
  advances only from a genuine outbound customer email posted on the
  lead (see ``_message_post_after_hook`` in crm_reply_status.py).

``_EARLY_STAGES`` is deliberately stage-NAME normalized (never raw ids);
the canonical target records are resolved by XMLID through the existing
PHASE 13 helper ``_premafirm_target_stages()``.
"""
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Only these early stages may move on a genuine inbound reply.  Later
# stages, folded stages (LOST, PAUSED / ON HOLD) and the won stage are
# never moved backwards by an inbound event.
_EARLY_STAGES = {'new / uncontacted', 'outreach sent'}

# Terminal / post-sale stages (§2.1 guard): the deal is won or the
# customer is already past the sale.  A genuine inbound reply must NEVER
# drag one of these back to an earlier / engagement stage — the reply
# takes the flag-and-activity branch instead (Needs Attention +
# deduplicated "Respond to customer" activity).  Matched on normalized
# stage NAME (stage records are configurable data, never hardcoded ids):
# canonical names from data/crm_stages.xml plus the legacy DB names the
# audit found in production (APPROVED, WAITING FOR LOADS).
_TERMINAL_STAGES = {
    'won / active customer', 'won', 'active customer',
    'approved', 'waiting for loads', 'onboarding',
}

_SALES_TEAM_XMLID = 'sales_team.team_sales_department'

# Configuration parameters (both optional; unset → base.user_admin).
_PARAM_NOTIFY_PARTNER = 'crm.new_lead.notify_partner_id'
_PARAM_DEFAULT_ASSIGNEE = 'crm.new_lead.default_salesperson_id'

# PHASE 14 "Respond to customer" activity type (crm_activity_discipline
# ``_TYPE_XMLIDS``) — the dedupe guard for the terminal-stage reply funnel.
_RESPOND_CUSTOMER_XMLID = (
    'premafirm_ai_engine.premafirm_activity_respond_customer')


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # ── shared helper (rule 4 / rule 1 guards) ───────────────────────

    def _outreach_has_external_recipient(self, message):
        """False when every recipient attached to ``message`` is an
        internal user.  Internal-only emails and notes never count as
        customer outreach, so they never stamp or move the lead.  A
        message with no recipient partner rows at all has no provably
        internal audience and keeps the legacy treatment."""
        if not message.partner_ids:
            return True
        return any(not p.user_ids.filtered(lambda u: not u.share)
                   for p in message.partner_ids)

    def _is_post_sale_stage(self):
        """True when the lead sits on a terminal / post-sale stage: the
        deal is won (structural ``is_won`` — covers any future won stage)
        or the customer is past the sale (WON / ACTIVE CUSTOMER, APPROVED,
        WAITING FOR LOADS, ONBOARDING — ``_TERMINAL_STAGES`` by normalized
        name).  Never true for LOST / PAUSED / ON HOLD (folded) stages."""
        stage = self.stage_id
        if not stage:
            return False
        return bool(stage.is_won) or (
            self._normalized_stage_name() in _TERMINAL_STAGES)

    def _open_respond_customer_activity(self):
        """Open (not archived/done) PHASE 14 'Respond to customer'
        activities on the lead.  ``activity_ids`` already excludes done
        activities (Odoo 18 archives them via ``active``), so this is the
        dedupe guard for the §2.1 terminal-stage reply funnel — an open
        Respond to customer means the reply was already funneled at route
        time (inbound_routing) and must not be duplicated or moved."""
        type_id = self.env.ref(_RESPOND_CUSTOMER_XMLID).id
        return self.activity_ids.filtered(
            lambda a: a.activity_type_id.id == type_id)

    # ── rule-1 twin: genuine inbound reply → ENGAGED / REPLIED ───────

    def prema_process_normal_reply(self):
        """Inbound-reply handling (replaces Studio 'CRM: Replied').

        Called by the coded automation on_message_received (prefiltered
        to normal_reply).  Moves ONLY early-stage leads to
        ENGAGED / REPLIED and routes the reply through the PHASE 14
        discipline funnel exactly once: an open "Respond to customer"
        activity means the reply was already handled (classifier route /
        portal hook) — it is never duplicated; when nothing is open yet
        the funnel creates it and raises the waiting-response flag /
        attention.  Terminal / post-sale leads (§2.1, ``_is_post_sale_stage``)
        NEVER move — they take the flag-and-activity branch only.
        Re-processing the same message is a no-op."""
        for lead in self.sudo():
            stage_name = lead._normalized_stage_name()
            if stage_name in _EARLY_STAGES:
                targets = lead._premafirm_target_stages()
                engaged = targets.get('engaged / replied')
                if engaged and lead.stage_id.id != engaged.id:
                    lead.write({'stage_id': engaged.id})
                    _logger.info(
                        'Issue 13: reply moved lead %s %r → ENGAGED / REPLIED',
                        lead.id, lead.name)
                if not lead._open_discipline_activities():
                    lead._on_meaningful_reply()
                continue
            if not lead._is_post_sale_stage():
                # Mid-pipeline (QUOTE SENT, NEGOTIATION …) and folded
                # (LOST, PAUSED / ON HOLD) stages: nothing to do here —
                # the reply was already funneled at route time and these
                # stages never move backwards.
                continue
            # §2.1 / §2.2 — terminal / post-sale guard: NEVER move the
            # stage (a reply after the sale is service, not
            # re-qualification).  The reply raises the Needs Attention
            # flag and guarantees the ONE deduplicated "Respond to
            # customer" activity.  When the classifier route already
            # funneled this reply an open Respond to customer exists and
            # this branch is a no-op.
            if not lead._open_respond_customer_activity():
                lead._on_meaningful_reply()
                _logger.info(
                    'Issue 13 / §2.1: reply on post-sale lead %s %r — '
                    'attention flag + Respond to customer (stage %r kept)',
                    lead.id, lead.name, lead.stage_id.name or '')

    # ── rule-2 twin: new Sales-team lead → notify + default assign ───

    def prema_process_new_sales_lead(self):
        """New-lead handling (replaces Studio 'Notify Ahmad of new Sales
        team leads').

        Sales-team leads only.  One internal notification to the
        configured owner partner (param ``crm.new_lead.notify_partner_id``,
        fallback base.user_admin's partner) and assignment of the
        configured default salesperson (param
        ``crm.new_lead.default_salesperson_id``, fallback base.user_admin)
        — and ONLY when the lead is still unassigned: an existing
        salesperson is never overwritten.  Runs on create, so the
        notification fires once per lead by construction."""
        icp = self.env['ir.config_parameter'].sudo()
        admin = self.env.ref('base.user_admin', raise_if_not_found=False)
        notify_partner_id = int(
            icp.get_param(_PARAM_NOTIFY_PARTNER, '0') or 0)
        if not notify_partner_id and admin:
            notify_partner_id = admin.partner_id.id
        assignee_id = int(icp.get_param(_PARAM_DEFAULT_ASSIGNEE, '0') or 0)
        if not assignee_id and admin:
            assignee_id = admin.id
        team = self.env.ref(_SALES_TEAM_XMLID, raise_if_not_found=False)
        if not team:
            team = self.env['crm.team'].search(
                [('name', '=ilike', 'Sales')], limit=1)

        for lead in self.sudo():
            if not lead.active:
                continue
            if team and lead.team_id.id != team.id:
                continue
            if assignee_id and not lead.user_id:
                lead.write({'user_id': assignee_id})
            if notify_partner_id:
                follower_ids = lead.message_follower_ids.partner_id.ids
                if notify_partner_id not in follower_ids:
                    lead.message_subscribe(partner_ids=[notify_partner_id])
                partner_name = lead.partner_name or lead.contact_name or ''
                lead.message_notify(
                    partner_ids=[notify_partner_id],
                    subject='New CRM lead: %s' % (lead.name or ''),
                    body=(
                        'New lead on the Sales team.%s\n'
                        'Lead: %s%s\n'
                        'Assignee: %s'
                        % (
                            '\nCompany: %s' % partner_name
                            if partner_name else '',
                            lead.id, lead.name or '',
                            lead.user_id.name or 'unassigned',
                        )))
