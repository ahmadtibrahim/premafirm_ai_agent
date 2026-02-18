import logging

from odoo import _, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.crm_dispatch_service import CRMDispatchService

_logger = logging.getLogger(__name__)


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _clean_body(self):
        self.ensure_one()
        messages = self.message_ids.sorted("date", reverse=True)
        for msg in messages:
            if msg.body:
                return html2plaintext(msg.body)
        return ""

    def action_ai_calculate(self):
        self.ensure_one()
        email_text = self._clean_body()
        if not email_text:
            raise UserError(_("No email content found on this lead."))

        try:
            result = CRMDispatchService(self.env).process_lead(self, email_text)
        except Exception as exc:
            _logger.exception("AI dispatch processing failed for lead %s", self.id)
            self.write({"ai_recommendation": _("AI dispatch engine failed gracefully: %s") % str(exc)})
            return True

        if result.get("warnings"):
            self.message_post(body="<br/>".join([_("Dispatch warnings:"), *result["warnings"]]))
        return True
