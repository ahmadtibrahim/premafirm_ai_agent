from collections import deque

from odoo import api, fields, models
from odoo.exceptions import UserError


class PremafirmLoad(models.Model):
    _name = "premafirm.load"
    _description = "PremaFirm Load"

    name = fields.Char(required=True, default="New")
    sale_order_id = fields.Many2one("sale.order", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="sale_order_id.company_id", store=True, readonly=True)
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
    stop_ids = fields.One2many(related="sale_order_id.opportunity_id.dispatch_stop_ids", readonly=True)
    reefer_required = fields.Boolean(related="sale_order_id.opportunity_id.reefer_required", readonly=True)
    reefer_setpoint_c = fields.Float(related="sale_order_id.opportunity_id.reefer_setpoint_c", readonly=True)
    hos_warning_text = fields.Char(related="sale_order_id.opportunity_id.hos_warning_text", readonly=True)

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
        self._allocate_pallets()
        return self.env.ref("premafirm_ai_engine.action_report_premafirm_load_pod").report_action(self)

    def _allocate_pallets(self):
        """Allocates pallets from pickups to deliveries for POD generation."""
        self.ensure_one()
        allocations = {}
        pickup_stack = deque()
        stops = self.stop_ids.sorted(lambda s: (s.sequence, s.id))

        for stop in stops:
            pallets = max(int(stop.pallets or 0), 0)
            if stop.stop_type == "pickup":
                pickup_stack.append(
                    {
                        "pickup": stop,
                        "remaining": pallets,
                    }
                )
                continue

            if stop.stop_type != "delivery":
                continue

            delivery_remaining = pallets
            delivery_allocations = []
            while delivery_remaining > 0:
                while pickup_stack and pickup_stack[-1]["remaining"] <= 0:
                    pickup_stack.pop()
                if not pickup_stack:
                    raise UserError(
                        "Delivery '%s' has no matching pickup with available pallets. "
                        "Please correct stop sequencing/pallet counts before generating POD."
                        % (stop.address or stop.name or stop.display_name)
                    )

                current = pickup_stack[-1]
                allocated = min(current["remaining"], delivery_remaining)
                current["remaining"] -= allocated
                delivery_remaining -= allocated
                delivery_allocations.append(
                    {
                        "pickup_id": current["pickup"].id,
                        "pickup": current["pickup"],
                        "pallets": allocated,
                    }
                )

            allocations[stop.id] = delivery_allocations

        return allocations

    def _get_pickup_for_delivery(self, delivery):
        """Returns pickup stop linked to this delivery using pallet allocation logic."""
        self.ensure_one()
        if not delivery or delivery.stop_type != "delivery":
            return self.env["premafirm.dispatch.stop"]
        allocations = self._allocate_pallets().get(delivery.id, [])
        if not allocations:
            raise UserError(
                "Delivery '%s' has no pickup allocation."
                % (delivery.address or delivery.name or delivery.display_name)
            )
        return allocations[0]["pickup"]

    def _get_delivery_allocations(self, delivery):
        self.ensure_one()
        if not delivery or delivery.stop_type != "delivery":
            return []
        return self._allocate_pallets().get(delivery.id, [])
