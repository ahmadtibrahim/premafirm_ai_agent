from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")

    pickup_city = fields.Char()
    delivery_city = fields.Char()

    total_pallets = fields.Integer()
    total_weight_lbs = fields.Float()
    total_distance_km = fields.Float()

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        vals.update(
            {
                "premafirm_po": self.premafirm_po,
                "premafirm_bol": self.premafirm_bol,
                "premafirm_pod": self.premafirm_pod,
                "invoice_origin": self.name,
            }
        )
        return vals
