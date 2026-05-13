from odoo import fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    snov_client_id = fields.Char(
        string="Snov.io Client ID",
        config_parameter="snov.client_id",
    )
    snov_client_secret = fields.Char(
        string="Snov.io Client Secret",
        config_parameter="snov.client_secret",
    )

    def action_test_snov_connection(self):
        self.ensure_one()
        from ..services.snov_service import SnovService
        result = SnovService(self.env).test_connection()
        if result is True:
            msg, mtype = "Snov.io connection successful.", "success"
        else:
            msg, mtype = f"Connection failed: {result}", "danger"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Snov.io", "message": msg, "type": mtype, "sticky": False},
        }
    outreach_followup_days_1 = fields.Integer(
        string="Follow-up 1 After (business days)",
        config_parameter="outreach.followup_days_1",
        default=3,
    )
    outreach_followup_days_2 = fields.Integer(
        string="Follow-up 2 After (business days)",
        config_parameter="outreach.followup_days_2",
        default=7,
    )
    outreach_auto_followup = fields.Boolean(
        string="Enable Automatic Follow-up",
        config_parameter="outreach.auto_followup",
    )
