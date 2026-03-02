from odoo import fields, models


class PremafirmAICorrection(models.Model):
    _name = "premafirm.ai.correction"
    _description = "Premafirm AI Manual Correction"
    _order = "create_date desc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    stop_id = fields.Many2one("premafirm.dispatch.stop", required=True, ondelete="cascade")
    old_load_id = fields.Many2one("premafirm.load")
    new_load_id = fields.Many2one("premafirm.load")
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    timestamp = fields.Datetime(default=fields.Datetime.now, required=True)
    reason = fields.Char()
