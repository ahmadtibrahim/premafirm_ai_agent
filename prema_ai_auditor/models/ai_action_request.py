import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError


class AIActionRequest(models.Model):
    _name = "prema.ai.action.request"
    _description = "Prema AI Action Request"
    _order = "create_date desc"

    token = fields.Char(required=True, copy=False, default=lambda self: str(uuid.uuid4()), index=True)
    model_name = fields.Char(required=True)
    record_id = fields.Integer(required=True)
    action_type = fields.Selection(
        [("write", "Write"), ("server_action", "Server Action")],
        required=True,
        default="write",
    )
    payload_json = fields.Json(required=True)
    requested_by = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    approved = fields.Boolean(default=False)
    expires_at = fields.Datetime(required=True)
    executed = fields.Boolean(default=False)

    _sql_constraints = [
        ("prema_ai_action_request_token_unique", "unique(token)", "Action request token must be unique."),
    ]

    @api.model
    def create_request(self, model_name, record_id, payload_json, action_type="write"):
        return self.create(
            {
                "model_name": model_name,
                "record_id": record_id,
                "action_type": action_type,
                "payload_json": payload_json,
                "expires_at": fields.Datetime.add(fields.Datetime.now(), minutes=5),
            }
        )

    def action_approve(self):
        self.ensure_one()
        if not self.env.user.has_group("prema_ai_auditor.group_prema_ai_master"):
            raise UserError("Only Prema AI Master Control users can approve actions.")
        if fields.Datetime.now() > self.expires_at:
            raise UserError("Approval token expired.")
        self.approved = True
