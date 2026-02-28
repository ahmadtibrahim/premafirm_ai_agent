from odoo import fields, models


class PremaAuditSession(models.Model):
    _name = "prema.audit.session"
    _description = "Prema AI Audit Session"

    name = fields.Char(required=True, default="Audit Session")
    state = fields.Selection(
        [("draft", "Draft"), ("running", "Running"), ("done", "Done")],
        default="draft",
        required=True,
    )
    started_at = fields.Datetime()
    completed_at = fields.Datetime()
    log_ids = fields.One2many("prema.audit.log", "session_id")
