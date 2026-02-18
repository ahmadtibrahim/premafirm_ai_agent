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
    final_rate = fields.Monetary(currency_field="company_currency_id")
    ai_recommendation = fields.Text()
    discount_percent = fields.Float()
    discount_amount = fields.Monetary(currency_field="company_currency_id")

    # Kept for compatibility with previous versions; stop-level product now drives pricing.
    product_id = fields.Many2one("product.product", string="Freight Product")

    po_number = fields.Char("Customer PO #")
    bol_number = fields.Char("BOL #")
    pod_reference = fields.Char("POD Reference")
    payment_terms = fields.Many2one("account.payment.term", string="Payment Terms")

    # Backward-compatible aliases used in previous releases.
    premafirm_po = fields.Char(related="po_number", store=True, readonly=False)
    premafirm_bol = fields.Char(related="bol_number", store=True, readonly=False)
    premafirm_pod = fields.Char(related="pod_reference", store=True, readonly=False)

    leave_yard_at = fields.Datetime("Leave Yard At")
    departure_time = fields.Datetime(related="leave_yard_at", store=True, readonly=False)

    inside_delivery = fields.Boolean()
    liftgate = fields.Boolean()
    detention_requested = fields.Boolean()
    reefer_required = fields.Boolean()
    pump_truck_required = fields.Boolean()

    assigned_vehicle_id = fields.Many2one("fleet.vehicle")

    dispatch_run_id = fields.Many2one("premafirm.dispatch.run")
    schedule_locked = fields.Boolean(default=False)
    schedule_conflict = fields.Boolean(default=False)
    ai_optimization_suggestion = fields.Text()


    @api.depends("suggested_rate", "final_rate")
    def _compute_discounts_from_final_rate(self):
        for lead in self:
            suggested = lead.suggested_rate or 0.0
            final = lead.final_rate or 0.0
            if not suggested:
                lead.discount_amount = 0.0
                lead.discount_percent = 0.0
                continue
            discount_amount = max(suggested - final, 0.0)
            lead.discount_amount = discount_amount
            lead.discount_percent = (discount_amount / suggested) * 100.0 if suggested else 0.0

    @api.onchange("final_rate", "suggested_rate")
    def _onchange_final_rate_discount(self):
        self._compute_discounts_from_final_rate()

    @api.onchange("discount_percent", "discount_amount", "suggested_rate")
    def _onchange_discount_to_final_rate(self):
        for lead in self:
            suggested = lead.suggested_rate or 0.0
            amount = lead.discount_amount or 0.0
            percent = lead.discount_percent or 0.0
            if percent:
                amount = suggested * (percent / 100.0)
            lead.final_rate = max(suggested - amount, 0.0)


    def write(self, vals):
        res = super().write(vals)
        if any(k in vals for k in ("final_rate", "suggested_rate")):
            self._compute_discounts_from_final_rate()
        return res

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
        country = (stop.country or "").upper()
        if country:
            return country in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
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
        is_reefer = (stop.service_type or "dry") == "reefer"
        key = (is_us, bool(stop.is_ftl))
        product_tmpl = self.env.ref(xmlids[key], raise_if_not_found=False)
        if not product_tmpl:
            return False

        product = product_tmpl.product_variant_id
        if is_reefer and product_tmpl.product_variant_ids:
            variant = product_tmpl.product_variant_ids.filtered(lambda p: "reefer" in (p.display_name or "").lower())[:1]
            if variant:
                product = variant
        return product

    def _assign_stop_products(self):
        for lead in self:
            stops = lead.dispatch_stop_ids.sorted("sequence")
            if not stops:
                continue

            pickup_count = len(stops.filtered(lambda s: s.stop_type == "pickup"))
            delivery_count = len(stops.filtered(lambda s: s.stop_type == "delivery"))
            is_ftl = len(stops) == 2 and pickup_count == 1 and delivery_count == 1

            for stop in stops:
                stop.is_ftl = bool(is_ftl)
                product = lead._get_correct_product(stop)
                if product:
                    stop.product_id = product.id
                    stop.freight_product_id = product.id

            first_product = stops[:1].product_id
            if first_product:
                lead.product_id = first_product.id

    def _prepare_order_values(self):
        self.ensure_one()
        pickup_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")[:1]
        delivery_stop = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")[:1]
        order_vals = {
            "partner_id": self.partner_id.id,
            "origin": self.name,
            "opportunity_id": self.id,
            "client_order_ref": self.po_number,
            "note": self.ai_recommendation,
            "validity_date": fields.Date.today() + timedelta(days=7),
            "premafirm_po": self.po_number,
            "premafirm_bol": self.bol_number,
            "premafirm_pod": self.pod_reference,
            "pickup_city": self._extract_city(pickup_stop.address),
            "delivery_city": self._extract_city(delivery_stop.address),
            "total_pallets": self.total_pallets,
            "total_weight_lbs": self.total_weight_lbs,
            "total_distance_km": self.total_distance_km,
            "payment_term_id": self.payment_terms.id,
        }

        usa_company = self.env["res.company"].search([("name", "ilike", "usa")], limit=1)
        canada_company = self.env["res.company"].search([("name", "ilike", "can")], limit=1)
        is_us_partner = self.partner_id.country_id.code == "US"
        company = usa_company if is_us_partner and usa_company else canada_company if canada_company else self.company_id
        order_vals["company_id"] = company.id

        journal_domain = [("type", "=", "sale"), ("company_id", "=", company.id)]
        journal_domain.append(("name", "ilike", "USA" if is_us_partner else "CAN"))
        order_vals["journal_id"] = self.env["account.journal"].search(journal_domain, limit=1).id
        order_vals["currency_id"] = self.env.ref("base.USD").id if is_us_partner else self.env.ref("base.CAD").id
        return order_vals

    def _create_order_lines(self, order):
        base_rate = self.final_rate or self.suggested_rate or 0.0
        stops = self.dispatch_stop_ids.sorted("sequence")
        rate_per_stop = base_rate / len(stops) if stops else 0.0
        for stop in stops:
            product = stop.product_id or stop.freight_product_id
            if not product:
                raise UserError("Each stop requires a service product.")
            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product.id,
                    "name": stop.address or product.display_name,
                    "product_uom_qty": stop.pallets or 1,
                    "price_unit": rate_per_stop,
                    "discount": self.discount_percent or 0.0,
                    "scheduled_date": stop.scheduled_datetime,
                    "stop_type": stop.stop_type,
                    "stop_address": stop.address,
                    "stop_map_url": stop.map_url,
                    "eta_datetime": stop.estimated_arrival,
                    "stop_distance_km": stop.distance_km,
                    "stop_drive_hours": stop.drive_hours,
                }
            )

        if self.liftgate:
            liftgate_tmpl = self.env.ref("premafirm_ai_engine.product_liftgate", raise_if_not_found=False)
            liftgate_product = liftgate_tmpl.product_variant_id if liftgate_tmpl else False
            if liftgate_product:
                self.env["sale.order.line"].create({"order_id": order.id, "product_id": liftgate_product.id, "name": "Liftgate", "product_uom_qty": 1, "price_unit": liftgate_product.list_price})
        if self.inside_delivery:
            inside_tmpl = self.env.ref("premafirm_ai_engine.product_inside_delivery", raise_if_not_found=False)
            inside_product = inside_tmpl.product_variant_id if inside_tmpl else False
            if inside_product:
                self.env["sale.order.line"].create({"order_id": order.id, "product_id": inside_product.id, "name": "Inside Delivery", "product_uom_qty": 1, "price_unit": inside_product.list_price})

    def action_create_sales_order(self):
        self.ensure_one()

        if not self.partner_id:
            raise UserError("A customer must be selected before creating a sales order.")
        if not self.dispatch_stop_ids:
            raise UserError("Add dispatch stops before creating a sales order.")

        self._assign_stop_products()
        order = self.env["sale.order"].create(self._prepare_order_values())
        self._create_order_lines(order)

        if self.po_number:
            order.action_confirm()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": order.id,
            "view_mode": "form",
        }

    def action_ai_optimize_schedule(self):
        self.ensure_one()
        from ..services.run_planner_service import RunPlannerService

        planner = RunPlannerService(self.env)
        suggestions = planner.optimize_insertion_for_lead(self)
        self.write({"ai_optimization_suggestion": suggestions.get("text"), "schedule_conflict": not suggestions.get("feasible")})
        return True

    # Backward-compatible button action.
    def action_create_quotation(self):
        return self.action_create_sales_order()
