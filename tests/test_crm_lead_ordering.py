from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestCrmLeadOrdering(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lead = cls.env["crm.lead"]
        cls.admin = cls.env.ref("base.user_admin")
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

    def test_recent_reply_stops_affecting_order_once_attention_is_cleared(self):
        now = fields.Datetime.now()
        oldest_idle = self._make_lead("Oldest Idle", self.contacted)
        handled_reply = self._make_lead("Handled Reply", self.contacted)
        needs_attention = self._make_lead("Needs Attention", self.contacted)

        oldest_idle.write({"x_last_outreach_at": now - timedelta(days=10)})
        handled_reply.write({
            "x_last_outreach_at": now - timedelta(days=1),
            "x_attention_at": now,
            "x_reply_received_at": now,
            "x_needs_attention": False,
        })
        needs_attention.write({
            "x_last_outreach_at": now - timedelta(days=2),
            "x_attention_at": now - timedelta(hours=1),
            "x_reply_received_at": now - timedelta(hours=1),
            "x_needs_attention": True,
            "x_attention_reason": "reply",
        })

        ordered = self.Lead.search(
            [("id", "in", (oldest_idle | handled_reply | needs_attention).ids)],
            order=self.Lead._order,
        )

        self.assertEqual(ordered.ids, [needs_attention.id, oldest_idle.id, handled_reply.id])
        self.assertFalse(handled_reply.x_attention_reply_sort_at)
        self.assertEqual(needs_attention.x_attention_reply_sort_at, needs_attention.x_reply_received_at)

    def test_outgoing_email_moves_new_lead_to_contacted_and_bottom(self):
        now = fields.Datetime.now()
        stale_contacted = self._make_lead("Stale Contacted", self.contacted)
        emailed_from_new = self._make_lead("Emailed From New", self.new)

        stale_contacted.write({"x_last_outreach_at": now - timedelta(days=7)})
        emailed_from_new.write({"x_last_outreach_at": now - timedelta(days=3)})

        emailed_from_new.message_post(
            author_id=self.admin.partner_id.id,
            body="Checking in with a fresh outbound email.",
            message_type="email",
            subtype_xmlid="mail.mt_comment",
        )

        self.assertEqual(emailed_from_new.stage_id, self.contacted)
        self.assertGreater(emailed_from_new.x_last_outreach_at, stale_contacted.x_last_outreach_at)

        ordered = self.Lead.search(
            [("id", "in", (stale_contacted | emailed_from_new).ids)],
            order=self.Lead._order,
        )
        self.assertEqual(ordered.ids, [stale_contacted.id, emailed_from_new.id])

    def test_internal_note_refreshes_sort_date_and_clears_attention(self):
        now = fields.Datetime.now()
        stale_contacted = self._make_lead("Stale Contacted Note", self.contacted)
        noted_lead = self._make_lead("Noted Lead", self.contacted)

        stale_contacted.write({"x_last_outreach_at": now - timedelta(days=8)})
        noted_lead.write({
            "x_last_outreach_at": now - timedelta(days=6),
            "x_attention_at": now - timedelta(days=1),
            "x_needs_attention": True,
            "x_attention_reason": "reply",
            "x_reply_received_at": now - timedelta(days=1),
        })

        noted_lead.message_post(
            author_id=self.admin.partner_id.id,
            body="Manual note logged after follow-up.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

        self.assertFalse(noted_lead.x_needs_attention)
        self.assertFalse(noted_lead.x_attention_reply_sort_at)
        self.assertFalse(noted_lead.x_attention_at)
        self.assertFalse(noted_lead.x_attention_reason)
        self.assertGreater(noted_lead.x_last_outreach_at, stale_contacted.x_last_outreach_at)

        ordered = self.Lead.search(
            [("id", "in", (stale_contacted | noted_lead).ids)],
            order=self.Lead._order,
        )
        self.assertEqual(ordered.ids, [stale_contacted.id, noted_lead.id])

    def test_assignment_to_new_owner_goes_to_top_with_attention(self):
        now = fields.Datetime.now()
        oldest_idle = self._make_lead("Oldest Idle Assignment", self.contacted)
        newly_assigned = self._make_lead("Newly Assigned", self.contacted)
        grace = self.env["res.users"].create({
            "name": "Grace CRM",
            "login": "grace.crm.assignment@example.com",
            "email": "grace.crm.assignment@example.com",
        })

        oldest_idle.write({"x_last_outreach_at": now - timedelta(days=9)})
        newly_assigned.write({
            "x_last_outreach_at": now - timedelta(days=1),
            "user_id": grace.id,
        })

        newly_assigned.write({"user_id": self.admin.id})

        self.assertTrue(newly_assigned.x_needs_attention)
        self.assertEqual(newly_assigned.x_attention_reason, "assignment")
        self.assertTrue(newly_assigned.x_attention_at)
        self.assertFalse(newly_assigned.x_reply_received_at)

        ordered = self.Lead.search(
            [("id", "in", (oldest_idle | newly_assigned).ids)],
            order=self.Lead._order,
        )
        self.assertEqual(ordered.ids, [newly_assigned.id, oldest_idle.id])
