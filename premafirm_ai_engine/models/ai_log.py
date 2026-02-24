from odoo import fields, models


class PremafirmAILog(models.Model):
    _name = "premafirm.ai.log"
    _description = "PremaFirm AI Billing Log"
    _order = "timestamp desc, id desc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    distance_km = fields.Float()
    pallets = fields.Integer()
    final_rate = fields.Float()
    user_modified = fields.Boolean(default=False)
    timestamp = fields.Datetime(default=fields.Datetime.now, required=True)
