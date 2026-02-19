from odoo import api, fields, models
from odoo.exceptions import UserError


class PremafirmLoad(models.Model):
    _name = "premafirm.load"
    _description = "PremaFirm Load"

    name = fields.Char(required=True, default="New")
    sale_order_id = fields.Many2one("sale.order", required=True, ondelete="cascade")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Vehicle")
    driver_id = fields.Many2one(
        "res.partner",
        related="vehicle_id.driver_id",
        store=True,
        readonly=True,
    )
    billing_mode = fields.Selection(
        related="sale_order_id.billing_mode",
        store=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="sale_order_id.currency_id",
        store=True,
    )
    total_amount = fields.Monetary(
        compute="_compute_total_amount",
        currency_field="currency_id",
    )

    route_reference = fields.Char(string="Route #")
    bol_number = fields.Char(string="BOL #")
    seal_number = fields.Char(string="Seal #")
    pickup_signature = fields.Binary()
    delivery_signature = fields.Binary()

    total_distance_km = fields.Float(related="sale_order_id.total_distance_km", store=True, readonly=True)
    total_pallets = fields.Integer(related="sale_order_id.total_pallets", store=True, readonly=True)

    @api.depends("billing_mode", "sale_order_id.amount_total")
    def _compute_total_amount(self):
        for load in self:
            load.total_amount = load.sale_order_id.amount_total if load.billing_mode == "flat" else 0.0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("premafirm.load") or "New"
        return super().create(vals_list)

    def action_generate_pod(self):
        self.ensure_one()
        if not self.vehicle_id:
            raise UserError("Vehicle must be assigned before generating POD.")
        if not self.driver_id:
            raise UserError("Driver must be assigned before generating POD.")
        return self.env.ref("premafirm_ai_engine.action_report_premafirm_load_pod").report_action(self)
