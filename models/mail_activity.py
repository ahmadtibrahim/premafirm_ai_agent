import logging

from odoo import models

_logger = logging.getLogger(__name__)

_DATA_COLLECTION_STAGE = "data collection"
_PROTECTED_STAGE_NAMES = frozenset({"replied", "onboarding", "call approach", "pause", "paused"})


class MailActivity(models.Model):
    _inherit = "mail.activity"

    def action_feedback(self, feedback=False, attachment_ids=None):
        return self._run_crm_lead_stage_guard(
            super().action_feedback,
            feedback=feedback,
            attachment_ids=attachment_ids,
        )

    def action_feedback_schedule_next(self, feedback=False, attachment_ids=None):
        return self._run_crm_lead_stage_guard(
            super().action_feedback_schedule_next,
            feedback=feedback,
            attachment_ids=attachment_ids,
        )

    def _run_crm_lead_stage_guard(self, action, **kwargs):
        protected_leads = self._snapshot_protected_crm_leads()
        result = action(**kwargs)
        self._restore_protected_crm_leads(protected_leads)
        return result

    def _snapshot_protected_crm_leads(self):
        protected = {}
        for activity in self:
            if activity.res_model != "crm.lead" or not activity.res_id:
                continue
            lead = self.env["crm.lead"].browse(activity.res_id).exists()
            if not lead or not lead.stage_id:
                continue
            if self._normalize_stage_name(lead.stage_id.name) not in _PROTECTED_STAGE_NAMES:
                continue
            protected[lead.id] = lead.stage_id.id
        return protected

    def _restore_protected_crm_leads(self, protected_leads):
        if not protected_leads:
            return
        lead_model = self.env["crm.lead"].sudo()
        for lead_id, stage_id in protected_leads.items():
            lead = lead_model.browse(lead_id).exists()
            if not lead or not lead.stage_id:
                continue
            if self._normalize_stage_name(lead.stage_id.name) != _DATA_COLLECTION_STAGE:
                continue
            target_stage = self.env["crm.stage"].sudo().browse(stage_id).exists()
            if not target_stage:
                continue
            try:
                lead.write({"stage_id": target_stage.id})
                _logger.info(
                    "CRM activity guard restored lead %s to stage %s after answered activity.",
                    lead.id,
                    target_stage.name,
                )
            except Exception as exc:
                _logger.warning(
                    "CRM activity guard failed to restore lead %s to stage %s: %s",
                    lead.id,
                    target_stage.id,
                    exc,
                )

    @staticmethod
    def _normalize_stage_name(stage_name):
        return (stage_name or "").strip().lower()
