from unittest.mock import patch

from odoo.addons.mail.models.mail_activity import MailActivity as MailActivityBase
from odoo.tests.common import TransactionCase


class TestMailActivityStageGuard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lead = cls.env["crm.lead"]
        cls.Activity = cls.env["mail.activity"]
        cls.Model = cls.env["ir.model"]
        cls.admin = cls.env.ref("base.user_admin")
        cls.todo = cls.env.ref("mail.mail_activity_data_todo")
        cls.crm_lead_model_id = cls.Model._get_id("crm.lead")

        # The guard restores leads that end up in the CANONICAL data-collection
        # stage ("qualified / data collected") after an answered activity —
        # legacy-named stages are not protected by design.
        cls.data_collection = cls.env["crm.stage"].search(
            [("name", "=ilike", "qualified / data collected")], limit=1)
        if not cls.data_collection:
            cls.data_collection = cls.env["crm.stage"].create(
                {"name": "QUALIFIED / DATA COLLECTED"})

    @classmethod
    def _get_stage(cls, name):
        # Case-insensitive match so canonical stage names ('ONBOARDING',
        # 'ENGAGED / REPLIED', …) are reused instead of duplicated; falls
        # back to creating the stage when the DB has no match.
        stage = cls.env["crm.stage"].search([("name", "=ilike", name)], limit=1)
        if not stage:
            stage = cls.env["crm.stage"].create({"name": name})
        return stage

    def _make_activity(self, lead):
        return self.Activity.create({
            "activity_type_id": self.todo.id,
            "res_id": lead.id,
            "res_model_id": self.crm_lead_model_id,
            "user_id": self.admin.id,
            "summary": "Follow up",
        })

    def test_action_feedback_restores_protected_stages(self):
        protected_stage_names = ["Replied", "ONboarding", "Call Approach", "Pause"]

        for stage_name in protected_stage_names:
            with self.subTest(stage_name=stage_name):
                protected_stage = self._get_stage(stage_name)
                lead = self.Lead.create({
                    "name": f"Lead {stage_name}",
                    "type": "opportunity",
                    "user_id": self.admin.id,
                    "stage_id": protected_stage.id,
                })
                activity = self._make_activity(lead)

                def fake_super(recordset, feedback=False, attachment_ids=None):
                    lead.write({"stage_id": self.data_collection.id})
                    return False

                with patch.object(MailActivityBase, "action_feedback", autospec=True, side_effect=fake_super):
                    activity.action_feedback()

                self.assertEqual(lead.stage_id, protected_stage)

    def test_action_feedback_allows_non_protected_stage_changes(self):
        original_stage = self._get_stage("Contacted")
        lead = self.Lead.create({
            "name": "Lead Contacted",
            "type": "opportunity",
            "user_id": self.admin.id,
            "stage_id": original_stage.id,
        })
        activity = self._make_activity(lead)

        def fake_super(recordset, feedback=False, attachment_ids=None):
            lead.write({"stage_id": self.data_collection.id})
            return False

        with patch.object(MailActivityBase, "action_feedback", autospec=True, side_effect=fake_super):
            activity.action_feedback()

        self.assertEqual(lead.stage_id, self.data_collection)
