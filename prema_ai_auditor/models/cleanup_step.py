from odoo import fields, models


class PremaCleanupStep(models.Model):
    _name = "prema.cleanup.step"
    _description = "AI Cleanup Plan Step"
    _order = "sequence asc, id asc"

    plan_id = fields.Many2one("prema.cleanup.plan", required=True, ondelete="cascade")
    sequence = fields.Integer(default=1)
    action_type = fields.Char(required=True)
    model_name = fields.Char(required=True)
    record_id = fields.Integer(required=True)
    description = fields.Text()
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
    approval_stage = fields.Integer(default=1, required=True)
    approved = fields.Boolean(default=False)
    executed = fields.Boolean(default=False)
