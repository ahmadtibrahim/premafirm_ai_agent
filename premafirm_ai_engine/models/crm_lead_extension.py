from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.crm_dispatch_service import CRMDispatchService


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

    product_id = fields.Many2one("product.product", string="Freight Product")

    premafirm_po = fields.Char("Customer PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD Reference")
    payment_terms = fields.Many2one("account.payment.term", string="Payment Terms")

    leave_yard_at = fields.Datetime("Leave Yard At")
    departure_time = fields.Datetime(related="leave_yard_at", store=True, readonly=False)

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

    def _extract_city(self, address):
        return (address.split(",", 1)[0] or "").strip() if address else ""

    def _is_us_stop(self, stop):
        address = (stop.address or "").upper()
        return "USA" in address or "UNITED STATES" in address or ", US" in address

    def _get_correct_product(self, stop):
        xmlids = {
            (True, True): "premafirm_ai_engine.product_ftl_usa",
            (True, False): "premafirm_ai_engine.product_ltl_usa",
            (False, True): "premafirm_ai_engine.product_ftl_can",
            (False, False): "premafirm_ai_engine.product_ltl_can",
        }
        is_us = self._is_us_stop(stop)
        key = (is_us, bool(stop.is_ftl))
        return self.env.ref(xmlids[key], raise_if_not_found=False)

    def _assign_stop_products(self):
        for lead in self:
            capacity = float(lead.assigned_vehicle_id.payload_capacity_lbs or 40000.0)
            inferred_capacity_pallets = max(1.0, capacity / 1800.0)

            for stop in lead.dispatch_stop_ids:
                if stop.stop_type == "pickup":
                    stop.is_ftl = float(lead.total_pallets or 0) >= inferred_capacity_pallets
                elif len(lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")) > 1:
                    stop.is_ftl = False
                product = lead._get_correct_product(stop)
                if product:
                    stop.freight_product_id = product.id

    def action_ai_calculate(self):
        for lead in self:
            CRMDispatchService(self.env)._apply_routes(lead)
            lead._assign_stop_products()
        return True

    def action_create_quotation(self):
        self.ensure_one()

        if not self.partner_id:
            raise UserError("A customer must be selected before creating a quotation.")

        if not self.dispatch_stop_ids:
            raise UserError("Add dispatch stops before creating a quotation.")

        self._assign_stop_products()
        pickup_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        delivery_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")[:1]

        order_vals = {
            "partner_id": self.partner_id.id,
            "origin": self.name,
            "opportunity_id": self.id,
            "client_order_ref": self.premafirm_po,
            "note": self.ai_recommendation,
            "validity_date": fields.Date.today() + timedelta(days=7),
            "premafirm_po": self.premafirm_po,
            "premafirm_bol": self.premafirm_bol,
            "premafirm_pod": self.premafirm_pod,
            "pickup_city": self._extract_city(pickup_stop.address),
            "delivery_city": self._extract_city(delivery_stop.address),
            "total_pallets": self.total_pallets,
            "total_weight_lbs": self.total_weight_lbs,
            "total_distance_km": self.total_distance_km,
            "payment_term_id": self.payment_terms.id,
        }

        if self.partner_id.country_id.code == "US":
            order_vals["fiscal_position_id"] = self.env["account.fiscal.position"].search([("name", "ilike", "US")], limit=1).id
            order_vals["journal_id"] = self.env["account.journal"].search([("name", "ilike", "US Sales"), ("type", "=", "sale")], limit=1).id
            order_vals["currency_id"] = self.env.ref("base.USD").id
        else:
            order_vals["currency_id"] = self.env.ref("base.CAD").id

        order = self.env["sale.order"].create(order_vals)

        base_rate = self.final_rate or self.suggested_rate or 0.0
        stops = self.dispatch_stop_ids.sorted("sequence")
        rate_per_stop = base_rate / len(stops) if stops else 0.0
        for stop in stops:
            product = stop.freight_product_id or self.product_id
            if not product:
                raise UserError("Each stop requires a freight product.")
            line_name = (
                f"{stop.stop_type.title()} | {stop.address or ''} | "
                f"Scheduled: {stop.scheduled_datetime or '-'} | ETA: {stop.eta_datetime or '-'} | "
                f"Distance: {stop.distance_km:.1f} KM | Drive: {stop.drive_hours:.2f} H"
            )
            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product.id,
                    "name": line_name,
                    "product_uom_qty": 1,
                    "price_unit": rate_per_stop,
                    "stop_type": stop.stop_type,
                    "stop_address": stop.address,
                    "scheduled_time": stop.scheduled_datetime,
                    "eta_datetime": stop.eta_datetime,
                    "stop_distance_km": stop.distance_km,
                    "stop_drive_hours": stop.drive_hours,
                }
            )

        if self.premafirm_po:
            order.action_confirm()

        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": order.id,
            "view_mode": "form",
        }
