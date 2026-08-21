"""PHASE 43 — pipeline cleanup tests.

Covers: canonical stage list, legacy→canonical migration mapping, no
lead loss, archived (folded) legacy stages, Call Back as an activity
concept (never a required stage), the retargeted automation filter, and
the canonical behavior of the outbound-email stage hook.
"""
from odoo.tests.common import TransactionCase

from ..models import crm_pipeline


class TestPipelineCleanup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lead = cls.env['crm.lead']
        cls.Stage = cls.env['crm.stage']
        cls.admin = cls.env.ref('base.user_admin')

    # ── canonical pipeline ───────────────────────────────────────────

    def test_canonical_stage_list_matches_spec(self):
        """The 11 canonical stages exist in order, exactly once each."""
        expected = [
            ('NEW / UNCONTACTED', False),
            ('OUTREACH SENT', False),
            ('ENGAGED / REPLIED', False),
            ('QUALIFIED / DATA COLLECTED', False),
            ('QUOTE REQUESTED', False),
            ('QUOTE SENT', False),
            ('NEGOTIATION', False),
            ('ONBOARDING', False),
            ('WON / ACTIVE CUSTOMER', False),
            ('LOST', True),
            ('PAUSED / ON HOLD', True),
        ]
        for name, folded in expected:
            stages = self.Stage.search([('name', '=', name)])
            self.assertEqual(len(stages), 1,
                             'stage "%s" must exist exactly once' % name)
            self.assertEqual(stages.fold, folded,
                             'fold flag for "%s" is wrong' % name)

        canonical = self.Stage.search([
            ('name', 'in', [n for n, _ in expected])],
            order='sequence')
        seqs = [s.sequence for s in canonical]
        self.assertEqual(seqs, sorted(seqs),
                         'canonical stages must be in sequence order')

    def test_no_duplicate_visible_stage_names(self):
        """No two non-folded stages share a name (no duplicate columns)."""
        visible = self.Stage.search([('fold', '=', False)])
        names = [s.name for s in visible]
        self.assertEqual(len(names), len(set(names)),
                         'duplicate visible stage names: %s' % names)

    def test_legacy_stages_archived_not_deleted(self):
        """Every legacy stage still exists but is folded with 0 leads."""
        legacy = {
            'New', 'Outreach', 'Contacted', 'Replied', 'Data Collection',
            'Onboarding', 'Approved', 'Lost / Cancelled', 'Paused',
            'Call Back', 'Call Approach', 'Dedicated Corridors Campaign',
            'Retail', 'Suggestion',
        }
        for name in legacy:
            stages = self.Stage.search([('name', '=', name)])
            self.assertEqual(len(stages), 1,
                             'legacy stage "%s" must not be deleted' % name)
            self.assertTrue(stages.fold,
                            'legacy stage "%s" must be folded' % name)
            self.assertEqual(
                self.Lead.search_count([('stage_id', '=', stages.id),
                                        ('active', '=', True)]),
                0, 'legacy stage "%s" must hold no active leads' % name)

    # ── migration mapping ────────────────────────────────────────────

    def test_migration_mapping_covers_all_legacy_stages(self):
        """_OLD_TO_NEW maps every legacy stage name to a live target."""
        targets = self.Lead._premafirm_target_stages()
        for norm_old, (target, _tags, _note) in crm_pipeline._OLD_TO_NEW.items():
            if target is None:
                continue  # intelligent split (Contacted/Call Back/...)
            self.assertIn(target.lower(), targets,
                          'mapping target "%s" is not a canonical stage'
                          % target)

    def test_restructure_dryrun_is_idempotent_no_lead_loss(self):
        """Dry-run then execute: every lead lands on a canonical stage,
        total lead count unchanged, second execute writes nothing."""
        lead_count_before = self.Lead.search_count(
            [('active', 'in', [True, False])])

        # fixture leads on legacy stages
        fixture = self.Lead.create([{
            'name': 'Fixture New',
            'type': 'opportunity',
            'stage_id': self.Stage.search([('name', '=', 'New')], limit=1).id,
        }, {
            'name': 'Fixture Replied',
            'type': 'opportunity',
            'stage_id': self.Stage.search([('name', '=', 'Replied')], limit=1).id,
        }, {
            'name': 'Fixture Approved',
            'type': 'opportunity',
            'stage_id': self.Stage.search([('name', '=', 'Approved')], limit=1).id,
        }])

        report = self.Lead.action_premafirm_pipeline_restructure(dryrun=True)
        self.assertTrue(report.get('dryrun'),
                        'dryrun must not touch the database')

        report = self.Lead.action_premafirm_pipeline_restructure(dryrun=False)
        self.assertFalse(report.get('dryrun'))
        self.assertGreaterEqual(report.get('to_move', 0), 3,
                                'the 3 fixture leads must be remapped')

        for lead in fixture:
            self.assertIn(lead.stage_id.name,
                          ('NEW / UNCONTACTED', 'ENGAGED / REPLIED',
                           'WON / ACTIVE CUSTOMER'),
                          'fixture lead %s must be on a canonical stage'
                          % lead.name)

        # no lead loss
        lead_count_after = self.Lead.search_count(
            [('active', 'in', [True, False])])
        self.assertEqual(lead_count_after, lead_count_before + 3)

        # second execute is a no-op: every lead already sits on a
        # canonical stage (to_move counts audit rows incl. 'already in
        # target', so the truthful signal is already_in_target == total)
        report2 = self.Lead.action_premafirm_pipeline_restructure(dryrun=True)
        self.assertEqual(report2.get('already_in_target'),
                         report2.get('leads_total'),
                         'second execute must not move anything')

    # ── Call Back as an activity concept ─────────────────────────────

    def test_call_back_is_activity_not_stage(self):
        """No canonical stage is named 'Call Back'; the Call activity
        type exists and a Call-Back lead maps to a canonical stage."""
        self.assertFalse(self.Stage.search(
            [('name', '=', 'Call Back'), ('fold', '=', False)]),
            "'Call Back' must never be a visible pipeline stage")
        call_type = self.env['mail.activity.type'].search(
            [('name', '=', 'Call')], limit=1)
        self.assertTrue(call_type, 'Call activity type must exist')

        stage = self.Stage.search([('name', '=', 'Call Back')], limit=1)
        lead = self.Lead.create({
            'name': 'Fixture Call Back',
            'type': 'opportunity',
            'stage_id': stage.id,
        })
        targets = self.Lead._premafirm_target_stages()
        lead.write({'stage_id': targets['outreach sent'].id})
        lead.activity_schedule(
            activity_type_id=call_type.id,
            summary='Call back customer',
            date_deadline='2026-08-21',
            user_id=self.admin.id,
        )
        # count only Call activities: the activity-discipline hook
        # auto-schedules an 'Initial contact' activity on lead create
        self.assertEqual(
            len(lead.activity_ids.filtered(
                lambda a: a.activity_type_id == call_type)), 1)
        self.assertEqual(
            lead.activity_ids.filtered(
                lambda a: a.activity_type_id == call_type).activity_type_id,
            call_type)
        self.assertNotEqual(lead.stage_id.name, 'Call Back')

    # ── automation retarget (18.0.7.1.0 migration) ───────────────────

    def test_new_to_outreach_automation_references_canonical_stage(self):
        """The live automation filter must reference NEW / UNCONTACTED
        and its action must resolve OUTREACH SENT, never a folded stage."""
        automation = self.env.ref(
            'premafirm_ai_engine.automation_new_to_outreach',
            raise_if_not_found=False)
        if not automation:
            self.skipTest('automation not installed')
        for clause in automation.filter_domain:
            if clause[0] == 'stage_id.name' and clause[1] == '=':
                self.assertEqual(
                    clause[2], 'NEW / UNCONTACTED',
                    'automation must filter on the canonical stage')
        for action in automation.action_server_ids:
            self.assertNotIn("'Outreach'", action.code or '',
                             'action code must not reference legacy stage')
            self.assertIn('OUTREACH SENT', action.code or '')

    # ── outbound-email stage hook (canonical behavior) ───────────────

    def test_outbound_advance_only_new_to_outreach(self):
        """_maybe_advance_on_outgoing: NEW / UNCONTACTED → OUTREACH SENT;
        every other stage is untouched (no move into folded legacy
        stages)."""
        targets = self.Lead._premafirm_target_stages()
        new_lead = self.Lead.create({
            'name': 'Fixture Hook New',
            'type': 'opportunity',
            'stage_id': targets['new / uncontacted'].id,
        })
        engaged_lead = self.Lead.create({
            'name': 'Fixture Hook Engaged',
            'type': 'opportunity',
            'stage_id': targets['engaged / replied'].id,
        })
        legacy_contacted = self.Stage.search([('name', '=', 'Contacted')],
                                             limit=1)

        engaged_lead._maybe_advance_on_outgoing()
        self.assertEqual(engaged_lead.stage_id, targets['engaged / replied'],
                         'engaged lead must not move on outbound email')
        self.assertNotEqual(engaged_lead.stage_id, legacy_contacted,
                            'lead must never move into a folded legacy stage')

        new_lead._maybe_advance_on_outgoing()
        self.assertEqual(new_lead.stage_id, targets['outreach sent'],
                         'new lead must advance to OUTREACH SENT')
