from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = "crm.lead"

    dispatch_stop_ids = fields.One2many("premafirm.dispatch.stop", "lead_id")

    total_pallets = fields.Integer(compute="_compute_dispatch_totals", store=True)
    total_weight_lbs = fields.Float(compute="_compute_dispatch_totals", store=True)
    total_distance_km = fields.Float(compute="_compute_dispatch_totals", store=True)
    total_drive_hours = fields.Float(compute="_compute_dispatch_totals", store=True)

    company_currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
        store=True,
    )

    estimated_cost = fields.Monetary(currency_field="company_currency_id")
    suggested_rate = fields.Monetary(currency_field="company_currency_id")
    final_rate = fields.Monetary(currency_field="company_currency_id", compute="_compute_final_rate", store=True)
    ai_recommendation = fields.Text()
    discount_percent = fields.Float()
    discount_amount = fields.Monetary(currency_field="company_currency_id")

    product_id = fields.Many2one("product.product", string="Freight Product", required=True)

    po_number = fields.Char("Customer PO #")
    bol_number = fields.Char("BOL #")
    pod_reference = fields.Char("POD Reference")

    departure_time = fields.Datetime("Leave Yard At")

    inside_delivery = fields.Boolean()
    liftgate = fields.Boolean()
    detention_requested = fields.Boolean()

    assigned_vehicle_id = fields.Many2one("fleet.vehicle")

    @api.depends("suggested_rate", "discount_percent", "discount_amount")
    def _compute_final_rate(self):
        for lead in self:
            rate = lead.suggested_rate or 0.0

            if lead.discount_percent:
                rate -= rate * (lead.discount_percent / 100.0)

            if lead.discount_amount:
                rate -= lead.discount_amount

            lead.final_rate = max(rate, 0.0)

    @api.depends(
        "dispatch_stop_ids.pallets",
        "dispatch_stop_ids.weight_lbs",
        "dispatch_stop_ids.distance_km",
        "dispatch_stop_ids.drive_hours",
    )
    def _compute_dispatch_totals(self):
        for lead in self:
            lead.total_pallets = int(sum(lead.dispatch_stop_ids.mapped("pallets")))
            lead.total_weight_lbs = sum(lead.dispatch_stop_ids.mapped("weight_lbs"))
            lead.total_distance_km = sum(lead.dispatch_stop_ids.mapped("distance_km"))
            lead.total_drive_hours = sum(lead.dispatch_stop_ids.mapped("drive_hours"))

    def action_create_quotation(self):
        self.ensure_one()

        if not self.product_id:
            raise UserError("Select a freight product first.")

        if not self.partner_id:
            raise UserError("A customer must be selected before creating a quotation.")

        pickup_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        delivery_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")[:1]

        pickup_city = pickup_stop.address.split(",", 1)[0].strip() if pickup_stop and pickup_stop.address else ""
        delivery_city = delivery_stop.address.split(",", 1)[0].strip() if delivery_stop and delivery_stop.address else ""

        template_name = "FTL Quote" if (self.total_pallets or 0) > 10 else "LTL Quote"
        template = self.env["sale.order.template"].search([("name", "=", template_name)], limit=1)

        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_id.id,
                "origin": self.name,
                "opportunity_id": self.id,
                "client_order_ref": self.po_number,
                "note": self.ai_recommendation,
                "validity_date": fields.Date.today() + timedelta(days=7),
                "sale_order_template_id": template.id,
            }
        )

        self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": self.product_id.id,
                "name": self.product_id.name,
                "product_uom_qty": 1,
                "price_unit": self.final_rate,
            }
        )

        order.write(
            {
                "premafirm_po": self.po_number,
                "premafirm_bol": self.bol_number,
                "premafirm_pod": self.pod_reference,
                "pickup_city": pickup_city,
                "delivery_city": delivery_city,
                "total_pallets": self.total_pallets,
                "total_weight_lbs": self.total_weight_lbs,
                "total_distance_km": self.total_distance_km,
            }
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": order.id,
            "view_mode": "form",
        }
