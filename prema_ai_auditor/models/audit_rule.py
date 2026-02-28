from odoo import fields, models


class PremaAuditRule(models.Model):
    _name = "prema.audit.rule"
    _description = "Prema AI Audit Rule"

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    active = fields.Boolean(default=True)
    severity = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        default="medium",
        required=True,
    )
    description = fields.Text()
