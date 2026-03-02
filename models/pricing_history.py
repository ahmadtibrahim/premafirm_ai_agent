from odoo import fields, models


class PremafirmPricingHistory(models.Model):
    _name = "premafirm.pricing.history"
    _description = "PremaFirm Pricing History"
    _order = "sent_date desc, id desc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    customer_id = fields.Many2one("res.partner", required=False)

    pickup_city = fields.Char()
    delivery_city = fields.Char()
    distance_km = fields.Float()

    pallets = fields.Integer()
    weight = fields.Float(help="Weight in lbs")

    final_price = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id.id,
        required=True,
    )

    sent_date = fields.Datetime(default=fields.Datetime.now, required=True)
