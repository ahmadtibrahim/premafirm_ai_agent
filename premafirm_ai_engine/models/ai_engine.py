from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.crm_dispatch_service import CRMDispatchService


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _clean_body(self):
        messages = self.message_ids.sorted("date", reverse=True)
        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue
            if msg.message_type not in ("email", "comment"):
                continue
            if msg.body:
                cleaned_html = (html2plaintext(msg.body) or "").strip()
                if cleaned_html:
                    return cleaned_html
        return ""

    def action_ai_calculate(self):
        self.ensure_one()
        email_text = self._clean_body()
        if not email_text:
            raise UserError("No email content found.")
        CRMDispatchService(self.env).process_lead(self, email_text)
        return True
