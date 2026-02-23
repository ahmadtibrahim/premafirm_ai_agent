from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.crm_dispatch_service import CRMDispatchService


class CrmLeadAI(models.Model):
    _inherit = "crm.lead"

    def _get_ai_attachments(self, message):
        attachments = message.attachment_ids if message else self.env["ir.attachment"]
        if attachments:
            return attachments
        return self.env["ir.attachment"].search(
            [
                ("res_model", "=", "crm.lead"),
                ("res_id", "=", self.id),
                ("name", "!=", False),
            ]
        ).filtered(lambda a: (a.name or "").lower().endswith((".pdf", ".xlsx", ".xls")))

    def _get_latest_email_message(self):
        messages = self.message_ids.sorted("date", reverse=True)
        load_keywords = ("load #", "# of pallets", "pickup", "delivery", "total weight")

        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue
            if msg.message_type != "email":
                continue
            body = html2plaintext(msg.body or "").lower()
            if msg.attachment_ids or any(keyword in body for keyword in load_keywords):
                return msg

        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue
            if msg.message_type == "email":
                return msg

        for msg in messages:
            if msg.model != "crm.lead" or msg.res_id != self.id:
                continue
            if msg.message_type == "comment":
                return msg
        return self.env["mail.message"]

    def _clean_body(self):
        msg = self._get_latest_email_message()
        if msg and msg.body:
            return (html2plaintext(msg.body) or "").strip()
        return ""

    def action_ai_calculate(self):
        self.ensure_one()
        if not self.billing_mode:
            raise UserError("Billing mode is required before AI Auto Calculate.")
        if (self.final_rate or 0.0) <= 0.0:
            raise UserError("Final rate is required before AI Auto Calculate.")
        msg = self._get_latest_email_message()
        email_text = self._clean_body()
        attachments = self._get_ai_attachments(msg)
        if not email_text and not attachments:
            raise UserError("No email content or attachments found.")
        CRMDispatchService(self.env).process_lead(self, email_text, attachments=attachments)
        return True
