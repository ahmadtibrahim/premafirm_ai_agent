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
  and WON records are never moved backwards.  The waiting-response flag
  and the one "Respond to customer" activity come from the PHASE 14
  funnel (``_on_meaningful_reply``), which is idempotent by design.
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

_SALES_TEAM_XMLID = 'sales_team.team_sales_department'

# Configuration parameters (both optional; unset → base.user_admin).
_PARAM_NOTIFY_PARTNER = 'crm.new_lead.notify_partner_id'
_PARAM_DEFAULT_ASSIGNEE = 'crm.new_lead.default_salesperson_id'


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
        attention.  Re-processing the same message is a no-op."""
        for lead in self.sudo():
            stage_name = lead._normalized_stage_name()
            if stage_name not in _EARLY_STAGES:
                continue
            targets = lead._premafirm_target_stages()
            engaged = targets.get('engaged / replied')
            if engaged and lead.stage_id.id != engaged.id:
                lead.write({'stage_id': engaged.id})
                _logger.info(
                    'Issue 13: reply moved lead %s %r → ENGAGED / REPLIED',
                    lead.id, lead.name)
            if not lead._open_discipline_activities():
                lead._on_meaningful_reply()

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
