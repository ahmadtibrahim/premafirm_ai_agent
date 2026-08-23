"""PHASE 14 — Activity / next-action discipline.

Every active opportunity must have an upcoming activity (or a documented
waiting state via ``next_followup_at``). The discipline is driven by the
lead's stage and the reply-status fields (PHASE 8):

* On stage entry the lead gets the stage's automatic activity —
  ``Respond to Customer`` when we owe a reply (``needs_reply``), otherwise
  the stage's follow-up (Initial Contact / Follow-Up / Gather Requirements /
  Quote Follow-Up / Negotiation Follow-Up / Onboarding Check).
* Reply received (``_on_meaningful_reply`` — wired at the same three
  meaningful-reply entry points as PHASE 10's ``_mark_replied``): the
  automatic follow-up(s) the reply answers are COMPLETED (never
  Call/Email/Meeting/To-Do — unrelated human tasks are untouched) and a
  Respond to Customer activity is scheduled.
* Sales response sent (``_on_sales_response`` — wired at the outbound
  paths): Respond to Customer activities are completed and the stage's
  next follow-up is scheduled.
* Timings are NEVER hardcoded — ``ir.config_parameter``:
  crm.followup.outreach_days (3), crm.followup.quote_days (5),
  crm.followup.engaged_hours (24), crm.followup.stale_days (7).
* ``_check_next_action_discipline`` reports active-stage leads with no
  open activity and no next_followup_at (the PHASE 16-17 dashboard and
  PHASE 15's Follow-Up Service consume it).
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# The discipline's own activity types (xmlids in the module's data).
_TYPE_XMLIDS = {
    'initial_contact': 'premafirm_ai_engine.premafirm_activity_initial_contact',
    'follow_up': 'premafirm_ai_engine.premafirm_activity_follow_up',
    'respond_customer': 'premafirm_ai_engine.premafirm_activity_respond_customer',
    'gather_requirements': 'premafirm_ai_engine.premafirm_activity_gather_requirements',
    'quote_follow_up': 'premafirm_ai_engine.premafirm_activity_quote_follow_up',
    'negotiation_follow_up': 'premafirm_ai_engine.premafirm_activity_negotiation_follow_up',
    'onboarding_check': 'premafirm_ai_engine.premafirm_activity_onboarding_check',
}

# Stage → (type key, config param, default amount, unit, summary).
# Lookup key = normalized (lowercased, stripped) stage NAME — stage records
# are configurable data, never hardcoded ids.
_FOLLOWUP_SPEC = {
    'new / uncontacted': ('initial_contact', 'crm.followup.outreach_days',
                          3, 'days', 'Initial contact — first touch'),
    'outreach sent': ('follow_up', 'crm.followup.outreach_days',
                      3, 'days', 'Follow up — outreach not answered'),
    'engaged / replied': ('follow_up', 'crm.followup.outreach_days',
                          3, 'days', 'Follow up — no response yet'),
    'qualified / data collected': ('gather_requirements',
                                   'crm.followup.quote_days', 5, 'days',
                                   'Gather requirements / prepare quote'),
    'quote requested': ('quote_follow_up', 'crm.followup.quote_days',
                        5, 'days', 'Quote follow-up'),
    'quote sent': ('quote_follow_up', 'crm.followup.quote_days',
                   5, 'days', 'Quote follow-up'),
    'negotiation': ('negotiation_follow_up', 'crm.followup.quote_days',
                    5, 'days', 'Negotiation follow-up'),
    'onboarding': ('onboarding_check', 'crm.followup.stale_days',
                   7, 'days', 'Onboarding check-in'),
}

_RESPOND_SPEC = ('respond_customer', 'crm.followup.engaged_hours',
                 24, 'hours', 'Respond to customer')


def _norm(name):
    return (name or '').strip().lower()


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # ── hooks ────────────────────────────────────────────────────────

    def write(self, vals):
        res = super().write(vals)
        if 'stage_id' in vals:
            self._ensure_stage_activity()
        return res

    def create(self, vals_list):
        leads = super().create(vals_list)
        leads._ensure_stage_activity()
        return leads

    # ── core ─────────────────────────────────────────────────────────

    def _premafirm_stage_spec(self):
        """(type_key, config_key, default, unit, summary) for the lead's
        stage, or None (won / folded / unmapped stages get no automatic
        activity — LOST, PAUSED / ON HOLD, WON / ACTIVE CUSTOMER …)."""
        self.ensure_one()
        if not self.stage_id or self.stage_id.fold or self.stage_id.is_won:
            return None
        return _FOLLOWUP_SPEC.get(_norm(self.stage_id.name))

    def _discipline_type_ids(self):
        return {self.env.ref(x).id for x in _TYPE_XMLIDS.values()}

    def _open_discipline_activities(self):
        auto = self._discipline_type_ids()
        return self.activity_ids.filtered(
            lambda a: a.activity_type_id.id in auto)

    def _schedule_discipline_activity(self, type_key, param_key, default,
                                      unit, summary):
        """Schedule one discipline activity (deduped on open same-type)
        and sync ``next_followup_at`` to its deadline."""
        self.ensure_one()
        type_id = self.env.ref(_TYPE_XMLIDS[type_key]).id
        if self.activity_ids.filtered(
                lambda a: a.activity_type_id.id == type_id):
            return False
        amount = int(self.env['ir.config_parameter'].sudo().get_param(
            param_key, str(default)) or default)
        deadline = fields.Datetime.now() + (
            timedelta(days=amount) if unit == 'days'
            else timedelta(hours=amount))
        user = self.user_id or self.env.user
        # Odoo 18: activity_schedule takes the TYPE XMLID string, not the
        # record id (it resolves via _xmlid_to_res_id internally).
        act = self.activity_schedule(
            _TYPE_XMLIDS[type_key], summary=summary,
            date_deadline=deadline.date(), user_id=user.id)
        self.next_followup_at = deadline
        _logger.info(
            'PHASE 14: scheduled %s activity on lead %s (deadline %s)',
            summary, self.id, deadline)
        return act

    def _ensure_stage_activity(self):
        """Discipline on stage entry: we owe a reply → Respond to
        Customer; otherwise the stage's follow-up. Never duplicates."""
        for lead in self:
            lead = lead.sudo()
            if not lead.active:
                continue  # archived leads get no automatic activity
            if not lead._premafirm_stage_spec():
                continue
            if lead.needs_reply:
                lead._schedule_discipline_activity(
                    *_RESPOND_SPEC)
            else:
                lead._schedule_discipline_activity(
                    *lead._premafirm_stage_spec())

    def _on_meaningful_reply(self):
        """Reply received (route-matched reply, portal comment, or
        human-confirmed queue attach). Completes ONLY the automatic
        follow-up activities the reply answers — Call/Email/Meeting/To-Do
        tasks are never touched — and schedules Respond to Customer.

        This is the single ATTENTION ACTIVATION funnel: any genuine
        customer reply raises Needs Attention (and refreshes the reply
        timestamps). An already-open Respond to Customer activity is never
        duplicated — only attention/timestamps refresh (dedup lives in
        _schedule_discipline_activity)."""
        for lead in self:
            lead = lead.sudo()
            open_auto = lead._open_discipline_activities()
            if open_auto:
                open_auto.action_feedback(
                    feedback='Customer replied — response scheduled.')
            lead._schedule_discipline_activity(*_RESPOND_SPEC)
            now = fields.Datetime.now()
            lead.write({'x_reply_received_at': now})
            lead._set_attention('reply', at=now)

    def _on_sales_response(self):
        """Response sent (composer post, AI reply wizard, bulk send).
        Completes open Respond to Customer activities and schedules the
        stage's next follow-up (needs_reply recomputes False because the
        outbound stamp write happens first).

        This is the single ATTENTION RESOLUTION funnel: an actual response
        from the salesperson is the only business event that clears Needs
        Attention. Safe on leads that were never flagged (no-op)."""
        for lead in self:
            lead = lead.sudo()
            respond_id = self.env.ref(
                _TYPE_XMLIDS['respond_customer']).id
            respond = lead.activity_ids.filtered(
                lambda a: a.activity_type_id.id == respond_id)
            if respond:
                respond.action_feedback(feedback='Answered — reply sent.')
            lead._ensure_stage_activity()
            lead._clear_attention()

    # ── discipline report ────────────────────────────────────────────

    @api.model
    def _check_next_action_discipline(self):
        """Active-stage leads (not won, not folded) with NO open activity
        AND no next_followup_at → violations. The report powers the
        PHASE 16-17 workspace views and PHASE 15's Follow-Up Service."""
        active_stages = self.env['crm.stage'].sudo().search([
            ('fold', '=', False), ('is_won', '=', False)])
        leads = self.sudo().search([('stage_id', 'in', active_stages.ids)],
                                   order='id')
        violations = []
        for lead in leads:
            if lead.activity_ids or lead.next_followup_at:
                continue
            violations.append({
                'lead_id': lead.id, 'lead_name': lead.name,
                'stage': lead.stage_id.name or '',
            })
        return {
            'total_active': len(leads),
            'with_action': len(leads) - len(violations),
            'violations': violations,
        }
