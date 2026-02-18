from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.crm_dispatch_service import CRMDispatchService


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _get_latest_email_message(self):
        messages = self.message_ids.sorted("date", reverse=True)
        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue
            if msg.message_type not in ("email", "comment"):
                continue
            return msg
        return self.env["mail.message"]

    def _clean_body(self):
        msg = self._get_latest_email_message()
        if msg and msg.body:
            return (html2plaintext(msg.body) or "").strip()
        return ""

    def action_ai_calculate(self):
        self.ensure_one()
        msg = self._get_latest_email_message()
        email_text = self._clean_body()
        attachments = msg.attachment_ids if msg else self.env["ir.attachment"]
        if not email_text and not attachments:
            raise UserError("No email content or attachments found.")
        CRMDispatchService(self.env).process_lead(self, email_text, attachments=attachments)
        return True
