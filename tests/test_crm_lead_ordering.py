"""PHASE 41 — pipeline wait-queue ordering.

The pipeline is a QUEUE: the lead that has waited the LONGEST since its
last meaningful CRM activity sits at the TOP of its stage, the most
recently touched lead at the BOTTOM.

- Meaningful activity: customer email, sales outbound, human note, reply.
- Noise (OdooBot chatter, mt_note tracking, notifications) does NOT reset
  the wait.
- Untouched leads fall back to create_date and rise to the top.
- x_needs_attention is a VISUAL badge only — it never controls ordering.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestCrmLeadOrdering(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lead = cls.env["crm.lead"]
        cls.admin = cls.env.ref("base.user_admin")
        cls.odoobot = cls.env.ref("base.partner_root")
        cls.contacted = cls._get_stage("Contacted")
        cls.new = cls._get_stage("New")

    @classmethod
    def _get_stage(cls, name):
        stage = cls.env["crm.stage"].search([("name", "=", name)], limit=1)
        if not stage:
            stage = cls.env["crm.stage"].create({"name": name})
        return stage

    def _make_lead(self, name, stage):
        return self.Lead.create({
            "name": name,
            "type": "opportunity",
            "user_id": self.admin.id,
            "stage_id": stage.id,
        })

    def _ordered(self, leads):
        return self.Lead.search(
            [("id", "in", leads.ids)],
            order=self.Lead._order,
        ).ids

    def test_oldest_meaningful_activity_first_newest_last(self):
        """Lead A waited longest (Aug 1), then B (Aug 5), then C (Aug 12).
        Oldest first, newest last — the whole point of the queue."""
        now = fields.Datetime.now()
        lead_a = self._make_lead("A waited since Aug 1", self.contacted)
        lead_b = self._make_lead("B waited since Aug 5", self.contacted)
        lead_c = self._make_lead("C waited since Aug 12", self.contacted)
        lead_a.write({"x_last_outreach_at": now - timedelta(days=12)})
        lead_b.write({"x_last_outreach_at": now - timedelta(days=8)})
        lead_c.write({"x_last_outreach_at": now - timedelta(days=1)})

        self.assertEqual(
            self._ordered(lead_a | lead_b | lead_c),
            [lead_a.id, lead_b.id, lead_c.id])

    def test_untouched_lead_rises_above_touched_leads(self):
        """A lead with NO meaningful activity is the most urgent: it uses
        create_date and must appear BEFORE leads touched since."""
        now = fields.Datetime.now()
        untouched = self._make_lead("Untouched (born waiting)", self.contacted)
        touched_later = self._make_lead("Touched later", self.contacted)
        # Backdate the untouched lead's birth: it has been waiting 20 days
        # while the other lead was engaged 1 day ago — the queue must put
        # the born-waiting lead ABOVE the recently-touched one.
        self.env.cr.execute(
            "UPDATE crm_lead SET create_date = %s WHERE id = %s",
            (now - timedelta(days=20), untouched.id))
        untouched.invalidate_recordset()
        self.env.add_to_compute(
            self.Lead._fields['x_meaningful_activity_at'], untouched)
        touched_later.write({"x_last_outreach_at": now - timedelta(days=1)})

        self.assertEqual(
            self._ordered(untouched | touched_later),
            [untouched.id, touched_later.id])
        self.assertEqual(
            untouched.x_meaningful_activity_at, untouched.create_date)

    def test_needs_attention_is_visual_only_and_never_reorders(self):
        """A lead with a fresh reply still carries the Needs Attention badge
        (x_needs_attention=True) but sorts BELOW an older idle lead."""
        now = fields.Datetime.now()
        old_idle = self._make_lead("Old idle, waited longest", self.contacted)
        fresh_reply = self._make_lead("Fresh reply today", self.contacted)
        old_idle.write({"x_last_outreach_at": now - timedelta(days=20)})
        fresh_reply.write({
            "x_reply_received_at": now - timedelta(hours=2),
            "x_needs_attention": True,
            "x_attention_at": now - timedelta(hours=2),
            "x_attention_reason": "reply",
        })

        self.assertTrue(fresh_reply.x_needs_attention)
        self.assertEqual(
            self._ordered(old_idle | fresh_reply),
            [old_idle.id, fresh_reply.id])

    def test_inbound_customer_email_refreshes_wait_timestamp(self):
        """A customer email is the strongest 'wait reset': it moves the lead
        DOWN the queue (most recently engaged)."""
        now = fields.Datetime.now()
        older = self._make_lead("Older engagement", self.contacted)
        just_replied = self._make_lead("Just replied", self.contacted)
        older.write({"x_last_outreach_at": now - timedelta(days=10)})
        just_replied.message_post(
            author_id=self.admin.partner_id.id,
            body="Fresh outbound email sent to the prospect.",
            message_type="email",
            subtype_xmlid="mail.mt_comment",
        )

        self.assertEqual(
            self._ordered(older | just_replied),
            [older.id, just_replied.id])
        self.assertGreater(
            just_replied.x_meaningful_activity_at,
            older.x_meaningful_activity_at)

    def test_system_tracking_note_does_not_refresh_wait(self):
        """System tracking noise (field-change logs, automation events) is
        posted as mt_note notifications — it must NOT reset the lead's age;
        the queue measures prospect waiting time."""
        now = fields.Datetime.now()
        stale = self._make_lead("Stale wait", self.contacted)
        stale.write({"x_last_outreach_at": now - timedelta(days=15)})
        before = stale.x_meaningful_activity_at

        # Real automations/fetchmail post system notifications as the acting
        # user with the mt_note subtype — message_type='notification' is
        # excluded both by the scan's type whitelist and by the
        # internal-note-activity check (which requires a human 'comment').
        stale.message_post(
            author_id=self.admin.partner_id.id,
            body="Probability updated by automation.",
            message_type="notification",
            subtype_xmlid="mail.mt_note",
        )

        self.assertEqual(stale.x_meaningful_activity_at, before)

    def test_odoobot_chatter_does_not_refresh_wait(self):
        """Automated OdooBot chatter (AI coaching, auto notes) never resets
        the prospect's wait time."""
        now = fields.Datetime.now()
        lead = self._make_lead("Bot chattered at me", self.contacted)
        lead.write({"x_last_outreach_at": now - timedelta(days=9)})
        before = lead.x_meaningful_activity_at

        lead.message_post(
            author_id=self.odoobot.id,
            body="Automated coaching note.",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )

        self.assertEqual(lead.x_meaningful_activity_at, before)

    def test_human_note_is_meaningful_interaction(self):
        """A manual human note (no field tracking) reflects the salesperson
        working the lead — it resets the wait (kept from Phase 3 behaviour)."""
        now = fields.Datetime.now()
        lead = self._make_lead("Human note", self.contacted)
        lead.write({"x_last_outreach_at": now - timedelta(days=6)})
        before = lead.x_meaningful_activity_at

        lead.message_post(
            author_id=self.admin.partner_id.id,
            body="Manual note logged after the call.",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )

        self.assertGreater(lead.x_meaningful_activity_at, before)

    def test_created_leads_sort_deterministically_by_create_date(self):
        """Same-stage untouched leads order by create_date, then id — no
        NULLs, no unstable default ordering."""
        a = self._make_lead("Untouched A", self.contacted)
        b = self._make_lead("Untouched B", self.contacted)
        self.assertEqual(
            self._ordered(a | b), [a.id, b.id])
        for lead in (a, b):
            self.assertTrue(lead.x_meaningful_activity_at)
