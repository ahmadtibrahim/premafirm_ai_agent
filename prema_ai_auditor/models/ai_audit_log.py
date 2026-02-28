from odoo import fields, models
from odoo.exceptions import UserError


class AIAuditLog(models.Model):
    _name = "prema.ai.audit.log"
    _description = "Prema AI Immutable Audit Log"
    _order = "create_date desc"

    action = fields.Char(required=True)
    status = fields.Selection(
        [("success", "Success"), ("failed", "Failed"), ("blocked", "Blocked")],
        required=True,
        default="success",
    )
    details = fields.Text()
    action_request_id = fields.Many2one("prema.ai.action.request", readonly=True)
    performed_by = fields.Many2one("res.users", readonly=True, default=lambda self: self.env.user)
    model_name = fields.Char(readonly=True)
    record_id = fields.Integer(readonly=True)

    def write(self, vals):
        raise UserError("Audit logs are immutable.")

    def unlink(self):
        raise UserError("Audit logs are immutable.")
