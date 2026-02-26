import logging
from odoo import models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.crm_dispatch_service import CRMDispatchService

_logger = logging.getLogger(__name__)


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

    def _get_inbound_email_messages(self):
        return self.env["mail.message"].search(
            [
                ("model", "=", "crm.lead"),
                ("res_id", "=", self.id),
                ("message_type", "=", "email"),
            ],
            order="date asc",
        )

    def _build_thread_text(self, messages):
        parts = []
        for msg in messages:
            body = (html2plaintext(msg.body or "") or "").strip()
            if body:
                parts.append(body)
        return "\n\n".join(parts).strip()

    def action_ai_calculate(self):
        for lead in self:
            confirmed_so = self.env["sale.order"].search_count(
                [
                    ("opportunity_id", "=", lead.id),
                    ("state", "not in", ["cancel"]),
                ]
            )
            if confirmed_so:
                raise UserError("AI locked after Sales Order creation.")

            messages = lead._get_inbound_email_messages()
            email_text = lead._build_thread_text(messages)
            attachments = messages.mapped("attachment_ids")
            if not attachments:
                attachments = lead._get_ai_attachments(False)
            if not email_text and not attachments:
                raise UserError("No email content or attachments found.")
            try:
                result = CRMDispatchService(self.env).process_lead(lead, email_text, attachments=attachments)
                lead.final_rate = result.get("pricing", {}).get("extracted_rate") or result.get("pricing", {}).get("final_rate") or lead.final_rate
            except UserError:
                raise
            except Exception as err:
                _logger.exception("AI scheduling failed for lead %s", lead.id)
                raise UserError(
                    f"AI Calculate failed. Technical reason: {type(err).__name__}. "
                    "Please check server logs for details."
                )
        return True
