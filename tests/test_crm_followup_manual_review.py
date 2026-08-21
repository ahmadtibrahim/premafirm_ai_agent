from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestCrmFollowupManualReview(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lead = cls.env["crm.lead"]
        cls.Activity = cls.env["mail.activity"]
        cls.admin = cls.env.ref("base.user_admin")
        # PHASE 43 — canonical stages only (xmlid resolution, same as the
        # cron code itself); legacy 'Outreach'/'Replied' are folded and
        # must never be the cron target.
        cls.outreach = cls.env.ref("premafirm_ai_engine.crm_stage_outreach_sent")
        cls.replied = cls.env.ref("premafirm_ai_engine.crm_stage_engaged_replied")

    def test_outreach_stale_cron_creates_manual_review_without_stage_move(self):
        lead = self.Lead.create({
            "name": "Stale Outreach Lead",
            "type": "opportunity",
            "user_id": self.admin.id,
            "stage_id": self.outreach.id,
            "x_response_status": "none",
            "x_last_outreach_at": fields.Datetime.now() - timedelta(days=8),
        })

        self.Lead.run_outreach_stale_cron()

        lead.invalidate_recordset(["stage_id"])
        self.assertEqual(lead.stage_id, self.outreach)
        activity = self.Activity.search([
            ("res_model", "=", "crm.lead"),
            ("res_id", "=", lead.id),
            ("summary", "=", "Review for manual Data Collection move"),
        ], limit=1)
        self.assertTrue(activity)

    def test_replied_stale_cron_creates_manual_review_without_stage_move(self):
        lead = self.Lead.create({
            "name": "Stale Replied Lead",
            "type": "opportunity",
            "user_id": self.admin.id,
            "stage_id": self.replied.id,
            "x_reply_received_at": fields.Datetime.now() - timedelta(days=7),
        })

        self.Lead.run_replied_stale_cron()

        lead.invalidate_recordset(["stage_id"])
        self.assertEqual(lead.stage_id, self.replied)
        activity = self.Activity.search([
            ("res_model", "=", "crm.lead"),
            ("res_id", "=", lead.id),
            ("summary", "=", "Review replied lead for manual Data Collection move"),
        ], limit=1)
        self.assertTrue(activity)
