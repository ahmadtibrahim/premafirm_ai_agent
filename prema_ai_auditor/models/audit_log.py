from odoo import fields, models


class PremaAuditLog(models.Model):
    _name = "prema.audit.log"
    _description = "Prema AI Audit Log"
    _order = "create_date desc"

    rule_name = fields.Char(required=True)
    severity = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        default="low",
        required=True,
    )
    model_name = fields.Char(required=True)
    record_id = fields.Integer(required=True)
    explanation = fields.Text()
    status = fields.Selection(
        [
            ("open", "Open"),
            ("approved", "Approved"),
            ("resolved", "Resolved"),
            ("rejected", "Rejected"),
        ],
        default="open",
        required=True,
    )
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    session_id = fields.Many2one("prema.audit.session")
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company, required=True
    )
