"""MP1 §2.7 (E-A3) — recurring-opportunity engine-side tests.

Covered (all external effects mocked — no mail ever leaves the DB):
  1. Create + open dedupe: one record per (lead, frequency, partner)
     while open; 'ended' releases the key.
  2. Potential → Contracted requires customer confirmation (and
     un-confirming a contracted record is refused).
  3. Activation never auto-generates anything: the engine has no hook —
     marking states active creates no logistics object and no activity.
  4. Follow-up scheduling: setting next_followup_date creates exactly ONE
     deduplicated activity; moving the date reschedules; clearing closes.
  5. State transitions are audit-logged (who/when in the chatter) and
     invalid transitions are refused.
  6. Dispatch-bridge sync contract (prema_sync_activation_from_dispatch):
     idempotent, anchor stored, guard requires contracted+confirmed.

Run targeted only, never the full suite, e.g.:

    odoo-bin -c <conf> -d <staging-db> -u premafirm_ai_engine \\
        --test-tags /recurring_opportunity --stop-after-init
"""
import unittest.mock as mock
from datetime import timedelta

from odoo.exceptions import UserError, ValidationError
from odoo.fields import Date
from odoo.tests import TransactionCase, tagged

from odoo.addons.mail.models.mail_mail import MailMail


@tagged('recurring_opportunity')
class TestRecurringOpportunity(TransactionCase):

    def setUp(self):
        super().setUp()
        # No real outbound delivery during tests.
        self.patcher = mock.patch.object(MailMail, 'send', autospec=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.partner = self.env['res.partner'].create({
            'name': 'Recurring Test Customer',
            'email': 'recurring-test@example.com',
            'is_company': True,
        })
        self.lead = self.env['crm.lead'].create({
            'name': 'Recurring Test Opportunity',
            'type': 'opportunity',
            'partner_id': self.partner.id,
        })

    # ── helpers ──────────────────────────────────────────────────────

    def _make(self, **kw):
        vals = {'lead_id': self.lead.id, 'frequency': 'weekly'}
        vals.update(kw)
        return self.env['crm.recurring.opportunity'].create(vals)

    def _open_followups(self, rec):
        act_type = self.env.ref(
            'premafirm_ai_engine.premafirm_activity_recurring_follow_up')
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'crm.recurring.opportunity'),
            ('res_id', '=', rec.id),
            ('activity_type_id', '=', act_type.id),
            ('active', '=', True),
        ])

    def _audit_bodies(self, rec):
        return [(m.body or '') for m in rec.message_ids]

    # ── 1. create + open dedupe ──────────────────────────────────────

    def test_create_open_dedupe(self):
        opp = self._make()
        with self.assertRaises(UserError):
            self._make()                                  # same key: refused
        with self.assertRaises(UserError):
            self._make(lead_id=self.lead.id,
                       partner_id=self.partner.id,
                       frequency='weekly')
        other_freq = self._make(frequency='biweekly')     # cadence differs
        self.assertNotEqual(opp.id, other_freq.id)
        # another partner on the same lead+cadence is a different key
        other_partner = self.env['res.partner'].create(
            {'name': 'Other Recurring Customer', 'is_company': True})
        other_lead = self.env['crm.lead'].create({
            'name': 'Other Opportunity', 'type': 'opportunity',
            'partner_id': other_partner.id})
        self._make(lead_id=other_lead.id)
        # ending releases the key
        opp.action_end()
        reopened = self._make()
        self.assertNotEqual(opp.id, reopened.id)
        self.assertEqual(reopened.activation_state, 'never_activated')

    # ── 2. contracted requires customer confirmation ────────────────

    def test_potential_to_contracted_requires_confirmation(self):
        opp = self._make()
        with self.assertRaises(ValidationError):
            opp.write({'kind': 'contracted'})
        with self.assertRaises(ValidationError):
            # create-time too (different frequency so the dedupe guard does
            # not pre-empt the confirmation constraint)
            self._make(kind='contracted', frequency='biweekly')
        # one write that carries the confirmation together is fine
        opp.write({
            'kind': 'contracted', 'customer_confirmed': True,
            'customer_confirmation_date': Date.today(),
        })
        self.assertEqual(opp.kind, 'contracted')
        # un-confirming a contracted record is refused…
        with self.assertRaises(ValidationError):
            opp.write({'customer_confirmed': False})
        # …but downgrading to potential first releases the confirmation
        opp.write({'kind': 'potential'})
        opp.write({'customer_confirmed': False})
        self.assertEqual(opp.customer_confirmed, False)

    def test_confirm_button_promotes_and_audits(self):
        opp = self._make()
        opp.action_confirm_customer()
        self.assertEqual(opp.kind, 'contracted')
        self.assertTrue(opp.customer_confirmed)
        self.assertTrue(opp.customer_confirmation_date)
        self.assertTrue(opp.customer_confirmation_user_id)
        self.assertTrue(any('Customer confirmation recorded' in body
                            for body in self._audit_bodies(opp)))
        # idempotent second click
        n = len(opp.message_ids)
        opp.action_confirm_customer()
        self.assertEqual(len(opp.message_ids), n)

    # ── 3. activation never auto-generates anything ─────────────────

    def test_activation_never_auto_generates(self):
        """Engine-side activation is bookkeeping only: no logistics call is
        possible (engine has no hook) — prove it by walking the whole state
        machine and asserting zero logistics rows and zero activities."""
        opp = self._make()
        opp.action_confirm_customer()
        opp.action_begin_verification()
        opp.action_mark_ready_activate()
        opp.action_mark_active()
        self.assertEqual(opp.activation_state, 'active')
        # no follow-up activity appeared by itself
        self.assertFalse(self._open_followups(opp))
        # if the dispatch recurring models are installed on this DB, they
        # must have been untouched — activation is explicitly non-generating
        for model in ('logistics.recurring.agreement',
                      'logistics.recurring.job'):
            if model in self.env.registry:
                self.assertEqual(
                    self.env[model].search_count(
                        [('partner_id', '=', self.partner.id)]), 0,
                    '%s must never be created by engine-side activation'
                    % model)
        # intent flag is inert bookkeeping
        opp.write({'intent_rate_confirmation': True})
        self.assertTrue(opp.intent_rate_confirmation)
        # invalid transitions are refused
        with self.assertRaises(UserError):
            opp.action_begin_verification()               # not never-activated
        with self.assertRaises(UserError):
            self._make().action_pause()                   # not active
        with self.assertRaises(UserError):
            self._make().action_mark_active()             # not awaiting/paused

    # ── 4. follow-up scheduling dedupe ──────────────────────────────

    def test_followup_scheduling_one_deduplicated_activity(self):
        d1 = Date.today()
        d2 = Date.today() + timedelta(days=3)
        opp = self._make()
        # setting the date schedules exactly one activity
        opp.write({'next_followup_date': d1})
        self.assertEqual(len(self._open_followups(opp)), 1)
        # re-setting the same date is a no-op (dedupe)
        opp.write({'next_followup_date': d1})
        self.assertEqual(len(self._open_followups(opp)), 1)
        # moving the date reschedules: still exactly one, new deadline
        opp.write({'next_followup_date': d2})
        acts = self._open_followups(opp)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts.date_deadline, d2)
        # clearing the date closes the open activity
        opp.write({'next_followup_date': False})
        self.assertFalse(self._open_followups(opp))

    def test_followup_scheduled_on_create(self):
        opp = self._make(next_followup_date=Date.today())
        acts = self._open_followups(opp)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts.date_deadline, Date.today())

    def test_followup_manual_button(self):
        opp = self._make()
        opp.action_schedule_followup_now()
        self.assertTrue(opp.next_followup_date)
        self.assertEqual(len(self._open_followups(opp)), 1)

    # ── 5. transitions audit-logged ─────────────────────────────────

    def test_state_transitions_audit_logged(self):
        opp = self._make()
        opp.action_begin_verification()
        bodies = self._audit_bodies(opp)
        self.assertTrue(any(
            'Activation state changed: Never Activated → Awaiting '
            'Verification' in body for body in bodies), bodies)
        opp.action_mark_ready_activate()
        bodies = self._audit_bodies(opp)
        self.assertTrue(any(
            'Activation state changed: Awaiting Verification → Awaiting '
            'Activation' in body for body in bodies), bodies)
        # the note carries the acting user (who + when)
        note = next(body for body in bodies
                    if 'Activation state changed' in body)
        self.assertIn(self.env.user.display_name
                      or self.env.user.login, note)
        opp.action_mark_active()
        self.assertEqual(opp.activation_state, 'active')
        self.assertTrue(opp.activated_at)
        # ended closes any open follow-up and is audit-logged too
        opp.action_pause()
        opp.action_end()
        self.assertEqual(opp.activation_state, 'ended')
        self.assertTrue(any('→ Ended' in body
                            for body in self._audit_bodies(opp)))
        # a second End is a safe no-op
        self.assertFalse(opp.action_end())

    # ── 6. dispatch-bridge sync contract ────────────────────────────

    def test_dispatch_sync_contract(self):
        opp = self._make()
        opp.action_confirm_customer()
        opp.action_begin_verification()
        opp.action_mark_ready_activate()
        self.assertEqual(opp.activation_state, 'awaiting_activation')

        # the bridge calls the sync event when its agreement activates
        changed = opp.prema_sync_activation_from_dispatch(
            'active', agreement_reference='CRM-REC-101')
        self.assertTrue(changed)
        self.assertEqual(opp.activation_state, 'active')
        self.assertTrue(opp.activated_at)
        self.assertEqual(opp.dispatch_agreement_reference, 'CRM-REC-101')

        # idempotent replay: no change, no extra audit note
        n = len(opp.message_ids)
        self.assertFalse(opp.prema_sync_activation_from_dispatch(
            'active', agreement_reference='CRM-REC-101'))
        self.assertEqual(len(opp.message_ids), n)

        # pause/expire mirror back onto the sales record
        opp.prema_sync_activation_from_dispatch('paused')
        self.assertEqual(opp.activation_state, 'paused')
        opp.prema_sync_activation_from_dispatch('expired')
        self.assertEqual(opp.activation_state, 'ended')
        self.assertTrue(any('agreement sync' in body
                            for body in self._audit_bodies(opp)))

    def test_dispatch_sync_guard(self):
        """Syncing a non-contracted / unconfirmed opportunity to Active is
        refused — the bridge must gate activation on confirmation."""
        opp = self._make()
        with self.assertRaises(UserError):
            opp.prema_sync_activation_from_dispatch('active')
        opp.action_confirm_customer()
        opp.prema_sync_activation_from_dispatch('active')   # now allowed
        self.assertEqual(opp.activation_state, 'active')
        with self.assertRaises(UserError):
            opp.prema_sync_activation_from_dispatch('bogus_state')

    # ── lead-side discovery ─────────────────────────────────────────

    def test_lead_smart_button_count_and_action(self):
        self.assertEqual(self.lead.recurring_opportunity_count, 0)
        opp = self._make()
        ended = self._make(frequency='monthly')
        ended.action_end()
        self.assertEqual(self.lead.recurring_opportunity_count, 1)
        action = self.lead.action_open_recurring_opportunities()
        self.assertEqual(action['res_model'], 'crm.recurring.opportunity')
        self.assertIn(('lead_id', '=', self.lead.id), action['domain'])
        self.assertEqual(action['context']['default_lead_id'], self.lead.id)
        # name builds from customer/cadence/kind
        self.assertIn(self.partner.name, opp.name)
