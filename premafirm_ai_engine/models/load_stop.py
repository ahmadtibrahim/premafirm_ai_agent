from odoo import models, fields


class PremafirmLoadStop(models.Model):
    _name = "premafirm.load.stop"
    _description = "Premafirm Load Stop"
    _order = "sequence asc"

    lead_id = fields.Many2one(
        "crm.lead",
        required=True,
        ondelete="cascade"
    )

    sequence = fields.Integer(default=1)

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")])

    address = fields.Char(required=True)
    pallets = fields.Integer()
    weight_lbs = fields.Float()
