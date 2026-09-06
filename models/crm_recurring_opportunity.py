"""MP1 §2.7 — Recurring-opportunity management (engine side, work package E-A3).

Ongoing weekly / biweekly / monthly / irregular customer work is captured as
``crm.recurring.opportunity`` records hanging off the crm.lead opportunity
(the one-to-many side: one deal can carry several recurring opportunities,
e.g. a biweekly LTL lane and a monthly FTL run).

Scope discipline — this file is ENGINE-only and must stay logistics-free:

* The engine module depends on NO dispatch module, so nothing here imports,
  calls or references logistics models. No booking / agreement generation
  exists or can exist on this side: generation belongs to the dispatch-side
  recurring engine (``logistics.recurring.agreement`` / ``.job``, activation
  gated, idempotent, created by the E-A3 dispatch companion commit).
* This model manages the SALES reality only: potential vs contracted cadence,
  customer confirmation, expected frequency/days/volumes, the activation
  state machine and the follow-up discipline. Activation is an EXPLICIT
  human action (or an explicit dispatch-side sync — see
  ``prema_sync_activation_from_dispatch`` and docs/A3_RECURRING_BRIDGE_CONTRACT.md).
  The "Create Rate Confirmation when activated" flag is an INTENT marker the
  dispatch bridge consumes; it never triggers anything by itself.
* Salesperson/assignment is never part of this model (assignment lives on
  the lead only — ``lead_id.user_id`` is never written here).
* One open record per (lead, expected-frequency, partner); ending releases
  the key (dedupe guard below).
* Every state change is audited in the chatter (who + when + from/to), on
  top of the ``tracking=True`` field history.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# ── selections ──────────────────────────────────────────────────────────
KIND = [
    ('potential', 'Potential'),
    ('contracted', 'Contracted'),
]
ACTIVATION_STATE = [
    ('never_activated', 'Never Activated'),
    ('awaiting_verification', 'Awaiting Verification'),
    ('awaiting_activation', 'Awaiting Activation'),
    ('active', 'Active'),
    ('paused', 'Paused'),
    ('ended', 'Ended'),
]
FREQUENCY = [
    ('weekly', 'Weekly'),
    ('biweekly', 'Every 2 Weeks'),
    ('monthly', 'Monthly'),
    ('irregular', 'Irregular'),
]
TEMPERATURE_MODE = [
    ('dry', 'Dry'),
    ('reefer', 'Reefer'),
]
LOAD_TYPE = [
    ('ltl', 'LTL'),
    ('ftl', 'FTL'),
]

# Weekday booleans follow the codebase convention (logistics.corridor
# ``operate_*`` day booleans) so the dispatch bridge can read them plainly.
_WEEKDAY_FIELDS = (
    ('preferred_monday', 'Mon'),
    ('preferred_tuesday', 'Tue'),
    ('preferred_wednesday', 'Wed'),
    ('preferred_thursday', 'Thu'),
    ('preferred_friday', 'Fri'),
    ('preferred_saturday', 'Sat'),
    ('preferred_sunday', 'Sun'),
)

# Follow-up activity type (data/crm_recurring_data.xml).
_FOLLOWUP_ACTIVITY_XMLID = (
    'premafirm_ai_engine.premafirm_activity_recurring_follow_up')
_FOLLOWUP_SUMMARY = 'Recurring follow-up'

# Dispatch agreement states → engine activation_state (prema_sync_...).
_DISPATCH_STATE_MAP = {
    'active': 'active',
    'paused': 'paused',
    'expired': 'ended',
    'cancelled': 'ended',
}


def _freq_label(value):
    return dict(FREQUENCY).get(value, value or '')


def _kind_label(value):
    return dict(KIND).get(value, value or '')


class CrmRecurringOpportunity(models.Model):
    _name = 'crm.recurring.opportunity'
    _description = 'Recurring Opportunity'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'next_followup_date asc, id desc'
    _rec_name = 'name'

    # ── identity ──────────────────────────────────────────────────────
    lead_id = fields.Many2one(
        'crm.lead', string='Opportunity', required=True, index=True,
        ondelete='cascade', tracking=True,
        help='The CRM opportunity/deal this recurring work belongs to. '
             'Assignment (salesperson) is read from the lead only and is '
             'never overwritten by this model.')
    partner_id = fields.Many2one(
        'res.partner', string='Customer', related='lead_id.partner_id',
        store=True, index=True, readonly=True,
        help='Customer company — mirrors the lead\'s partner so the '
             'dedupe key and the list columns work without joins.')
    name = fields.Char(compute='_compute_name', store=True)

    # ── potential vs contracted cadence (§2.7) ────────────────────────
    kind = fields.Selection(
        KIND, string='Cadence Kind', default='potential', required=True,
        tracking=True,
        help='Potential: ongoing-work possibility still under discussion. '
             'Contracted: the customer AGREED the recurring cadence — only '
             'reachable once customer confirmation is recorded below.')
    customer_confirmed = fields.Boolean(
        'Customer Confirmed', tracking=True,
        help='The customer explicitly confirmed the recurring arrangement '
             '(call/email). Contracted cadence requires this flag.')
    customer_confirmation_date = fields.Date('Customer Confirmation Date')
    customer_confirmation_user_id = fields.Many2one(
        'res.users', string='Customer Confirmation By', readonly=True)

    # ── expected cadence ──────────────────────────────────────────────
    frequency = fields.Selection(
        FREQUENCY, string='Expected Frequency', default='weekly',
        required=True, tracking=True,
        help='Weekly / biweekly / monthly / irregular. One open record per '
             '(lead, frequency, customer); end the previous record before '
             're-opening the same cadence.')
    frequency_detail = fields.Char(
        'Frequency Detail',
        help='Free-text precision: e.g. "every other Tuesday", "1st & 3rd '
             'week", or for irregular work describe the pattern.')
    preferred_monday = fields.Boolean('Monday')
    preferred_tuesday = fields.Boolean('Tuesday')
    preferred_wednesday = fields.Boolean('Wednesday')
    preferred_thursday = fields.Boolean('Thursday')
    preferred_friday = fields.Boolean('Friday')
    preferred_saturday = fields.Boolean('Saturday')
    preferred_sunday = fields.Boolean('Sunday')
    preferred_days_display = fields.Char(
        'Preferred Days', compute='_compute_preferred_days_display',
        help='Read-only summary of the preferred weekdays.')
    start_date = fields.Date('Effective Start Date')
    end_date = fields.Date('Effective End Date')
    next_followup_date = fields.Date(
        'Next Follow-up', tracking=True,
        help='Next sales follow-up. Setting a date schedules exactly one '
             'deduplicated "Recurring follow-up" activity on this record; '
             'clearing it closes the open one.')
    followup_overdue = fields.Boolean(
        'Follow-up Overdue', compute='_compute_followup_overdue',
        help='True when next_followup_date is set and lies in the past.')

    # ── expected shipment profile (informative; consumed by the bridge) ─
    expected_pallets = fields.Integer('Expected Pallets')
    expected_weight_lbs = fields.Float('Expected Weight (lb)')
    expected_temperature_mode = fields.Selection(
        TEMPERATURE_MODE, string='Expected Temperature Mode')
    required_temperature_c = fields.Float('Required Temperature (°C)')
    expected_equipment = fields.Char(
        'Expected Equipment', help='e.g. Reefer 0°C, Van, Flatbed …')
    expected_load_type = fields.Selection(
        LOAD_TYPE, string='Expected Load Type', default='ltl')
    commodity = fields.Char('Commodity')

    # ── activation state machine (§2.7 / §17.1-17.2) ───────────────────
    activation_state = fields.Selection(
        ACTIVATION_STATE, string='Activation State',
        default='never_activated', required=True, tracking=True,
        help='never_activated → awaiting_verification → awaiting_activation '
             '→ active, with paused / ended. Activation itself is an '
             'explicit human action or an explicit sync from the '
             'dispatch-side agreement engine — nothing on the engine side '
             'ever auto-generates logistics work.')
    activated_at = fields.Datetime('Activated At', copy=False, tracking=True)

    # "Create Rate Confirmation when activated" — INTENT flag (§17.1-17.2).
    # The dispatch bridge reads it when its agreement reaches Active; it
    # never triggers anything from the engine side.
    intent_rate_confirmation = fields.Boolean(
        'Create Rate Confirmation when Activated', default=False,
        help='Intent marker for the dispatch recurring bridge: when the '
             'linked recurring agreement is activated, the dispatcher '
             'should also produce the customer Rate Confirmation.')
    agreement_reference = fields.Char(
        'Customer Contract / PO',
        help='The customer\'s own contract/PO reference for this recurring '
             'work (forwarded to logistics.recurring.agreement.'
             'agreement_reference by the dispatch bridge).')
    dispatch_agreement_reference = fields.Char(
        'Linked Dispatch Agreement Ref', copy=False, tracking=True,
        readonly=True,
        help='Anchor back-reference written by the dispatch bridge when the '
             'logistics recurring agreement is created/activated '
             '(format "CRM-REC-<id> …"). Informational only — the real '
             'reverse link lives on the dispatch side.')

    notes = fields.Text('Notes / Why tracked')

    # ── computed ──────────────────────────────────────────────────────

    @api.depends('partner_id.name', 'lead_id.name', 'frequency', 'kind')
    def _compute_name(self):
        for rec in self:
            parts = [part for part in (
                rec.partner_id.name or (rec.lead_id.name or '')[:40],
                _freq_label(rec.frequency),
                _kind_label(rec.kind),
            ) if part]
            rec.name = ' — '.join(parts) or _('Recurring Opportunity')

    @api.depends('preferred_monday', 'preferred_tuesday',
                 'preferred_wednesday', 'preferred_thursday',
                 'preferred_friday', 'preferred_saturday', 'preferred_sunday')
    def _compute_preferred_days_display(self):
        for rec in self:
            rec.preferred_days_display = ', '.join(
                label for field_name, label in _WEEKDAY_FIELDS
                if rec[field_name]) or 'Not set'

    @api.depends('next_followup_date')
    def _compute_followup_overdue(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.followup_overdue = bool(
                rec.next_followup_date and rec.next_followup_date < today)

    def _preferred_weekday_indexes(self):
        """Preferred days as integer indexes (0=Monday .. 6=Sunday) — the
        same convention the corridor/day machinery uses."""
        labels = {'preferred_monday': 0, 'preferred_tuesday': 1,
                  'preferred_wednesday': 2, 'preferred_thursday': 3,
                  'preferred_friday': 4, 'preferred_saturday': 5,
                  'preferred_sunday': 6}
        return sorted(idx for name, idx in labels.items()
                      for rec in self if rec[name])

    # ── validation ────────────────────────────────────────────────────

    @api.constrains('kind', 'customer_confirmed')
    def _check_contracted_confirmed(self):
        """Contracted cadence is only legal with recorded customer
        confirmation (§2.7: contracted only after customer confirmation)."""
        for rec in self:
            if rec.kind == 'contracted' and not rec.customer_confirmed:
                raise ValidationError(
                    _('Cadence kind "Contracted" requires customer '
                      'confirmation: click "Customer Confirmed" (or set the '
                      'flag with date) before marking the cadence '
                      'contracted.'))

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if (rec.start_date and rec.end_date
                    and rec.end_date < rec.start_date):
                raise ValidationError(
                    _('Effective End Date must be on or after Effective '
                      'Start Date.'))

    def _raise_if_duplicate(self, lead_id, partner_id, frequency,
                            exclude_id=False):
        """One OPEN recurring opportunity per (lead, cadence, customer).
        A record whose activation_state is 'ended' releases the key."""
        domain = [
            ('lead_id', '=', lead_id),
            ('partner_id', '=', partner_id),
            ('frequency', '=', frequency),
            ('activation_state', '!=', 'ended'),
        ]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        existing = self.search(domain, limit=1)
        if existing:
            raise UserError(
                _('A recurring opportunity for this customer and cadence '
                  'already exists (%s — %s, %s). End that record first if '
                  'the old arrangement is truly over.')
                % (existing.name, _freq_label(existing.frequency),
                   existing.activation_state))

    # ── create/write hooks ────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        Lead = self.env['crm.lead']
        for vals in vals_list:
            lead_id = vals.get('lead_id')
            partner_id = vals.get('partner_id')
            if not partner_id and lead_id:
                lead = Lead.browse(lead_id)
                partner_id = lead.partner_id.id
            if not lead_id:
                raise UserError(
                    _('A recurring opportunity must be attached to a CRM '
                      'opportunity (lead).'))
            self._raise_if_duplicate(
                lead_id, partner_id or False,
                vals.get('frequency', 'weekly'))
        records = super().create(vals_list)
        records.filtered('next_followup_date')._schedule_followup()
        return records

    def write(self, vals):
        # Dedupe guard when the key fields move (re-keying a record onto a
        # key that another open record already holds must fail).
        if 'lead_id' in vals or 'frequency' in vals or 'partner_id' in vals:
            for rec in self:
                self._raise_if_duplicate(
                    vals.get('lead_id', rec.lead_id.id),
                    vals.get('partner_id', rec.partner_id.id),
                    vals.get('frequency', rec.frequency),
                    exclude_id=rec.id)
        old_followups = {
            rec.id: rec.next_followup_date for rec in self}
        result = super().write(vals)
        # Follow-up discipline: setting/advancing next_followup_date
        # schedules ONE deduplicated activity; clearing it closes the open
        # one; moving it reschedules (close old, schedule new).
        if 'next_followup_date' in vals:
            for rec in self:
                old = old_followups.get(rec.id)
                if rec.next_followup_date == old:
                    continue
                if rec.next_followup_date:
                    rec._schedule_followup(force=bool(old))
                else:
                    rec._close_open_followups(
                        feedback='Follow-up date cleared.')
        return result

    # ── follow-up discipline (dedupe per codebase convention) ──────────

    def _open_followup_activities(self):
        """Open 'Recurring follow-up' activities on these records."""
        act_type = self.env.ref(_FOLLOWUP_ACTIVITY_XMLID)
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', 'in', self.ids),
            ('activity_type_id', '=', act_type.id),
            ('active', '=', True),
        ])

    def _schedule_followup(self, force=False):
        """Schedule ONE deduplicated follow-up activity per record, at
        ``next_followup_date``, for the lead's salesperson (never assigned
        here). Returns the created activities (empty when deduped)."""
        created = self.env['mail.activity']
        if not self:
            return created
        act_type_id = self.env.ref(_FOLLOWUP_ACTIVITY_XMLID)
        for rec in self:
            if not rec.next_followup_date:
                continue
            open_acts = self.env['mail.activity'].sudo().search([
                ('res_model', '=', rec._name),
                ('res_id', '=', rec.id),
                ('activity_type_id', '=', act_type_id.id),
                ('active', '=', True),
            ])
            if open_acts and not force:
                continue  # already followed up — never a second activity
            if open_acts and force:
                open_acts.action_feedback(
                    feedback='Follow-up rescheduled to %s.'
                    % rec.next_followup_date)
            user = rec.lead_id.user_id or self.env.user
            act = rec.activity_schedule(
                _FOLLOWUP_ACTIVITY_XMLID,
                summary=_FOLLOWUP_SUMMARY,
                note=rec.name or _FOLLOWUP_SUMMARY,
                date_deadline=rec.next_followup_date,
                user_id=user.id,
            )
            if act:
                created |= act
                _logger.info(
                    'Recurring opportunity %s: follow-up scheduled for %s '
                    '(user %s)', rec.id, rec.next_followup_date, user.id)
        return created

    def _close_open_followups(self, feedback=''):
        open_acts = self._open_followup_activities()
        if open_acts:
            open_acts.action_feedback(
                feedback=feedback or 'Follow-up completed.')

    def action_schedule_followup_now(self):
        """Explicit header-button (re)schedule using next_followup_date;
        when no date is set yet, default to today."""
        today = fields.Date.context_today(self)
        for rec in self:
            if not rec.next_followup_date:
                rec.next_followup_date = today
        self._schedule_followup(force=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Follow-up Scheduled',
                'message': 'One follow-up activity was scheduled on %s.'
                % fields.Date.context_today(self),
                'type': 'success',
                'sticky': False,
            },
        }

    # ── audit helper ──────────────────────────────────────────────────

    def _audit(self, body):
        """Internal chatter note recording WHO changed WHAT and WHEN.
        ``tracking=True`` fields additionally leave per-field history."""
        for rec in self:
            rec.message_post(
                body=body + '<br/>— %s, %s' % (
                    self.env.user.display_name or self.env.user.login,
                    fields.Datetime.now().strftime('%Y-%m-%d %H:%M')),
                subtype_xmlid='mail.mt_note',
            )

    # ── customer confirmation ─────────────────────────────────────────

    def action_confirm_customer(self):
        """Record the customer's explicit confirmation and (for a potential
        cadence) promote it to contracted. Idempotent — a second click on
        an already-confirmed contracted record changes nothing and posts
        no duplicate audit note."""
        now = fields.Date.context_today(self)
        for rec in self:
            if rec.customer_confirmed and rec.kind == 'contracted':
                continue
            rec.write({
                'customer_confirmed': True,
                'customer_confirmation_date': rec.customer_confirmation_date
                or now,
                'customer_confirmation_user_id':
                self.env.user.id,
            })
            if rec.kind == 'potential':
                rec.write({'kind': 'contracted'})
            rec._audit(
                '<b>Customer confirmation recorded</b> — customer agreed '
                'the recurring cadence (kind: %s).'
                % _kind_label(rec.kind))
        return True

    # ── activation state transitions (explicit human actions) ─────────

    def _transition_activation(self, target, allowed, action_label):
        """Shared transition core: validates, writes, audits (who/when)."""
        self.ensure_one()
        old = self.activation_state
        if old not in allowed:
            raise UserError(
                _('Cannot %s a "%s" recurring opportunity — only from %s.')
                % (action_label, old,
                   ' / '.join(dict(ACTIVATION_STATE)[s] for s in allowed)))
        if target == old:
            return False  # idempotent
        if target == 'ended':
            self._close_open_followups(feedback='Recurring opportunity ended.')
        vals = {'activation_state': target}
        if target == 'active' and not self.activated_at:
            vals['activated_at'] = fields.Datetime.now()
        self.write(vals)
        self._audit(
            '<b>Activation state changed: %s → %s</b> (explicit %s).'
            % (dict(ACTIVATION_STATE)[old], dict(ACTIVATION_STATE)[target],
               action_label))
        return True

    def action_begin_verification(self):
        """Never Activated → Awaiting Verification (human start of work)."""
        return self._transition_activation(
            'awaiting_verification', ('never_activated',),
            'human action "Start Verification"')

    def action_mark_ready_activate(self):
        """Awaiting Verification → Awaiting Activation (verification done,
        waiting for the explicit activation decision)."""
        return self._transition_activation(
            'awaiting_activation', ('awaiting_verification',),
            'human action "Mark Ready to Activate"')

    def action_mark_active(self):
        """Awaiting Activation / Paused → Active (explicit activation —
        e.g. the dispatch-side agreement has been activated; when the
        bridge is in use it calls ``prema_sync_activation_from_dispatch``
        instead and this button is the manual-only path)."""
        return self._transition_activation(
            'active', ('awaiting_activation', 'paused'),
            'human action "Mark Active"')

    def action_pause(self):
        """Active → Paused (suspended)."""
        return self._transition_activation(
            'paused', ('active',), 'human action "Pause"')

    def action_end(self):
        """Any non-ended state → Ended. Releases the dedupe key and closes
        any open follow-up."""
        if self.activation_state == 'ended':
            return False
        return self._transition_activation(
            'ended', tuple(s for s, _ in ACTIVATION_STATE
                           if s != 'ended'),
            'human action "End"')

    # ── dispatch bridge event (§17.1-17.2 / docs contract) ────────────

    def prema_sync_activation_from_dispatch(self, agreement_state,
                                            agreement_reference=None,
                                            reason=''):
        """Activation event the DISPATCH recurring engine hooks.

        Called by the E-A3 dispatch companion when its linked
        logistics.recurring.agreement changes state (active / paused /
        expired / cancelled). The engine side NEVER generates logistics
        work — it only mirrors the agreement state back onto the sales
        record, records the anchor reference and audits the change.

        :param agreement_state: the logistics.recurring.agreement state
            ('active', 'paused', 'expired', 'cancelled').
        :param agreement_reference: the agreement's agreement_reference
            anchor (the bridge stores "CRM-REC-<id> …" there). Stored on
            first sync; never cleared afterwards.
        :param reason: optional human-readable context from the bridge.
        :return: True when the record changed, False when already in sync
            (idempotent).
        :raises UserError: when syncing to Active while the cadence is not
            contracted-with-confirmation — the bridge must gate activation
            on the CRM opportunity's confirmation first.
        """
        target = _DISPATCH_STATE_MAP.get(agreement_state)
        if not target:
            raise UserError(
                _('Unknown dispatch agreement state %r — cannot sync '
                  'recurring opportunity %s.')
                % (agreement_state, self.id))
        if target == 'active':
            for rec in self:
                if rec.kind != 'contracted' or not rec.customer_confirmed:
                    raise UserError(
                        _('Cannot activate recurring opportunity %s via the '
                          'dispatch agreement: the cadence must be '
                          'Contracted with customer confirmation recorded '
                          'first.') % rec.id)
        changed = False
        for rec in self:
            old = rec.activation_state
            if old == target:
                # Already in sync (idempotent) — only the anchor may still
                # be missing on the first idempotent replay.
                if agreement_reference and \
                        not rec.dispatch_agreement_reference:
                    rec.dispatch_agreement_reference = agreement_reference
                continue
            vals = {'activation_state': target}
            if target == 'active' and not rec.activated_at:
                vals['activated_at'] = fields.Datetime.now()
            if agreement_reference:
                vals['dispatch_agreement_reference'] = agreement_reference
            rec.write(vals)
            rec._audit(
                '<b>Activation state changed: %s → %s</b> — dispatch '
                'agreement sync (agreement state "%s")%s.'
                % (dict(ACTIVATION_STATE)[old],
                   dict(ACTIVATION_STATE)[target], agreement_state,
                   ' — %s' % reason if reason else ''))
            changed = True
            _logger.info(
                'Recurring opportunity %s: dispatch sync %s → %s (ref %s)',
                rec.id, old, target, agreement_reference or '-')
        return changed


class CrmLeadRecurring(models.Model):
    """crm.lead side of the recurring-opportunity relationship — a smart
    button on the opportunity form opens the deduplicated list, and the
    count shows the OPEN recurring opportunities (ended ones excluded)."""
    _inherit = 'crm.lead'

    recurring_opportunity_ids = fields.One2many(
        'crm.recurring.opportunity', 'lead_id',
        string='Recurring Opportunities')
    recurring_opportunity_count = fields.Integer(
        'Open Recurring Opportunities',
        compute='_compute_recurring_opportunity_count')

    @api.depends('recurring_opportunity_ids.activation_state')
    def _compute_recurring_opportunity_count(self):
        for lead in self:
            lead.recurring_opportunity_count = \
                len(lead.recurring_opportunity_ids.filtered(
                    lambda r: r.activation_state != 'ended'))

    def action_open_recurring_opportunities(self):
        """Smart-button drill-down: list+form of this lead's recurring
        opportunities, new records pre-bound to the lead (partner comes
        from the lead automatically)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Recurring Opportunities',
            'res_model': 'crm.recurring.opportunity',
            'view_mode': 'list,form',
            'domain': [('lead_id', '=', self.id)],
            'context': {'default_lead_id': self.id,
                        'search_default_open': 1},
            'help': '<p class="o_view_nocontent_smiling_face">Track ongoing '
                    'weekly / biweekly / monthly / irregular work for this '
                    'opportunity — potential or contracted.</p>',
        }
