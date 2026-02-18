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

    load_reference = fields.Char()

    def action_generate_pod(self):
        self.ensure_one()
        return self.env.ref("premafirm_ai_engine.action_report_premafirm_pod").report_action(self)

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        vals.update(
            {
                "ref": self.premafirm_po,
                "premafirm_po": self.premafirm_po,
                "premafirm_bol": self.premafirm_bol,
                "premafirm_pod": self.premafirm_pod,
                "load_reference": self.load_reference,
                "payment_reference": self.client_order_ref,
                "invoice_origin": self.name,
            }
        )
        return vals


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")])
    stop_address = fields.Char()
    scheduled_time = fields.Datetime()
    eta_datetime = fields.Datetime()
    stop_distance_km = fields.Float()
    stop_drive_hours = fields.Float()
