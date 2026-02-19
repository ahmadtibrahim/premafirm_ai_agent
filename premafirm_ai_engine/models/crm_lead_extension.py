import json
import re
from datetime import timedelta

from zoneinfo import ZoneInfo

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
    reefer_setpoint_c = fields.Float("Reefer Setpoint (°C)")
    pump_truck_required = fields.Boolean()
    ai_warning_text = fields.Text()
    hos_warning_text = fields.Char(compute="_compute_hos_warning_text", store=True)

    assigned_vehicle_id = fields.Many2one("fleet.vehicle")

    billing_mode = fields.Selection(
        [
            ("flat", "Flat"),
            ("per_km", "Per KM"),
            ("per_pallet", "Per Pallet"),
            ("per_stop", "Per Stop"),
        ],
        default="flat",
        required=True,
    )
    equipment_type = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")
    service_type = fields.Selection([("ftl", "FTL"), ("ltl", "LTL")], default="ftl")
    ai_override_command = fields.Text()
    ai_locked = fields.Boolean(default=False)
    ai_internal_summary = fields.Text()
    ai_customer_email = fields.Text()
    pricing_payload_json = fields.Text(readonly=True)

    pickup_date = fields.Date()
    delivery_date = fields.Date()
    ai_classification = fields.Selection([("ftl", "FTL"), ("ltl", "LTL")], default="ftl")

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


    @api.depends("total_drive_hours")
    def _compute_hos_warning_text(self):
        for lead in self:
            lead.hos_warning_text = "Driver hours exceed recommended HOS threshold." if (lead.total_drive_hours or 0.0) > 11.0 else False

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

    def _get_pallet_mismatch_warning(self):
        self.ensure_one()
        pickups = sum(max(int(s.pallets or 0), 0) for s in self.dispatch_stop_ids if s.stop_type == "pickup")
        deliveries = sum(max(int(s.pallets or 0), 0) for s in self.dispatch_stop_ids if s.stop_type == "delivery")
        if pickups != deliveries:
            return "Pallet mismatch warning: picked %s pallets, deliveries total %s pallets." % (pickups, deliveries)
        return False

    def _extract_city(self, address):
        return (address.split(",", 1)[0] or "").strip() if address else ""

    def _is_us_stop(self, stop):
        country = (stop.country or "").upper()
        if country:
            return country in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
        address = (stop.address or "").upper()
        return "USA" in address or "UNITED STATES" in address or ", US" in address

    def _get_service_product(self):
        self.ensure_one()
        country_code = (self.partner_id.country_id.code or "").upper()
        region = "usa" if country_code == "US" else "canada"
        service_label = "FTL" if (self.service_type or "ftl") == "ftl" else "LTL"
        region_label = "USA" if region == "usa" else "Canada"
        equipment = (self.equipment_type or "dry").lower()

        template = self.env["product.template"].search(
            [
                ("name", "ilike", service_label),
                ("name", "ilike", "Freight Service"),
                ("name", "ilike", region_label),
            ],
            limit=1,
        )
        if not template:
            template = self.env["product.template"].search([("name", "ilike", "Freight Service")], limit=1)
        if not template:
            return False

        if equipment == "reefer":
            reefer_variant = template.product_variant_ids.filtered(lambda p: "reefer" in (p.display_name or "").lower())[:1]
            if reefer_variant:
                return reefer_variant
        return template.product_variant_id

    def _create_ai_log(self, user_modified=False):
        self.ensure_one()
        self.env["premafirm.ai.log"].create(
            {
                "lead_id": self.id,
                "billing_mode": self.billing_mode,
                "distance_km": self.total_distance_km,
                "pallets": self.total_pallets,
                "final_rate": self.final_rate,
                "user_modified": user_modified,
                "timestamp": fields.Datetime.now(),
            }
        )

    def compute_pricing(self):
        for lead in self:
            if lead.billing_mode == "flat" and (lead.final_rate or 0.0) <= 0.0:
                raise UserError("Flat mode requires final_rate > 0.")
            if lead.billing_mode == "flat" and (lead.total_distance_km or 0.0) <= 0.0:
                raise UserError("Total distance must be greater than zero for flat mode.")

            stops = lead.dispatch_stop_ids.sorted("sequence")
            mismatch_warning = lead._get_pallet_mismatch_warning()
            rate = max(lead.final_rate or lead.suggested_rate or 0.0, 0.0)
            if rate < 0.0:
                raise UserError("Pricing cannot be negative.")

            payload = []
            delivery_stops = stops.filtered(lambda s: s.stop_type == "delivery")
            total_km = sum(max(stop.distance_km or 0.0, 0.0) for stop in delivery_stops) or lead.total_distance_km or 0.0
            per_km_rate = (rate / total_km) if lead.billing_mode == "flat" and total_km else (rate if lead.billing_mode == "per_km" else 0.0)
            per_pallet_rate = rate if lead.billing_mode == "per_pallet" else 0.0
            per_stop_rate = (rate / len(delivery_stops)) if lead.billing_mode == "per_stop" and delivery_stops else 0.0

            flat_delivery_ids = [stop.id for stop in delivery_stops]
            for stop in stops:
                segment_km = max(stop.distance_km or 0.0, 0.0)
                pallets = max(stop.pallets or 0, 0)
                if lead.billing_mode == "flat":
                    if stop.id in flat_delivery_ids:
                        segment_rate = segment_km * per_km_rate
                    else:
                        segment_rate = 0.0
                elif lead.billing_mode == "per_km":
                    segment_rate = segment_km * per_km_rate
                elif lead.billing_mode == "per_pallet":
                    segment_rate = per_pallet_rate * pallets
                else:
                    segment_rate = per_stop_rate if stop.stop_type == "delivery" else 0.0
                payload.append(
                    {
                        "stop_id": stop.id,
                        "sequence": stop.sequence,
                        "stop_type": stop.stop_type,
                        "segment_km": segment_km,
                        "pallets": pallets,
                        "segment_rate": max(segment_rate, 0.0),
                    }
                )

            if lead.billing_mode == "flat" and flat_delivery_ids:
                delivery_payload = [item for item in payload if item["stop_type"] == "delivery"]
                rounded_running = 0.0
                for item in delivery_payload[:-1]:
                    item["segment_rate"] = round(item["segment_rate"], 2)
                    rounded_running += item["segment_rate"]
                delivery_payload[-1]["segment_rate"] = round(max(rate - rounded_running, 0.0), 2)

            bullets = [
                f"• Billing mode: {lead.billing_mode}",
                f"• Service type: {lead.service_type.upper()} | Equipment: {lead.equipment_type.upper()}",
                f"• Total distance: {total_km:.2f} km",
                f"• Final rate basis: {rate:.2f}",
            ]
            for item in payload:
                bullets.append(
                    f"• Stop {item['sequence']} ({item['stop_type']}): {item['segment_km']:.2f} km => {item['segment_rate']:.2f}"
                )
            total_segment_amount = sum(x["segment_rate"] for x in payload)
            bullets.append(f"• Computed total: {total_segment_amount:.2f}")

            lead.write(
                {
                    "pricing_payload_json": json.dumps(payload),
                    "ai_internal_summary": "\n".join(bullets),
                    "ai_customer_email": f"Quoted in {lead.billing_mode} mode. Total {total_segment_amount:.2f}.",
                    "ai_warning_text": mismatch_warning,
                }
            )
            lead._create_ai_log(user_modified=False)
        return True

    def action_ai_override(self):
        for lead in self:
            if lead.ai_locked:
                raise UserError("AI override is locked after sales order confirmation.")
            command = (lead.ai_override_command or "").strip()
            if not command:
                continue
            vals = {}
            amount_match = re.search(r"(?:\$|rate\s*)(\d+(?:\.\d+)?)", command, re.I)
            if amount_match:
                vals["final_rate"] = float(amount_match.group(1))
            lowered = command.lower()
            if "per pallet" in lowered:
                vals["billing_mode"] = "per_pallet"
            elif "per stop" in lowered:
                vals["billing_mode"] = "per_stop"
            elif "per km" in lowered:
                vals["billing_mode"] = "per_km"
            elif "flat" in lowered:
                vals["billing_mode"] = "flat"

            if "reefer" in lowered:
                vals["equipment_type"] = "reefer"
            elif "dry" in lowered:
                vals["equipment_type"] = "dry"

            if "usa" in lowered:
                country = lead.env.ref("base.us", raise_if_not_found=False)
                if country and lead.partner_id:
                    lead.partner_id.country_id = country.id
            elif "canada" in lowered:
                country = lead.env.ref("base.ca", raise_if_not_found=False)
                if country and lead.partner_id:
                    lead.partner_id.country_id = country.id

            if vals:
                lead.write(vals)
            lead.compute_pricing()
            lead._create_ai_log(user_modified=True)
            lead.ai_override_command = False
        return True


    def action_reset_ai(self):
        for lead in self:
            if lead.ai_locked:
                continue
            lead.write(
                {
                    "billing_mode": "flat",
                    "service_type": "ftl",
                    "equipment_type": "dry",
                    "final_rate": 0.0,
                    "ai_override_command": False,
                    "ai_internal_summary": False,
                    "ai_customer_email": False,
                    "ai_recommendation": False,
                    "ai_optimization_suggestion": False,
                    "ai_warning_text": False,
                    "pricing_payload_json": False,

                    "dispatch_stop_ids": [(5, 0, 0)],

                }
            )
        return True

    def _default_pickup_datetime_toronto(self):
        tz = ZoneInfo("America/Toronto")
        now_utc = fields.Datetime.now()
        if getattr(now_utc, "tzinfo", None) is None:
            now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
        now_local = now_utc.astimezone(tz)
        pickup_local = now_local.replace(hour=9, minute=0, second=0, microsecond=0)
        if now_local.hour >= 12:
            pickup_local += timedelta(days=1)
        return pickup_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def classify_load(self, email_text=None, extracted_data=None):
        data = extracted_data or {}
        pickups = int(data.get("pickup_locations_count") or 0)
        deliveries = int(data.get("delivery_locations_count") or 0)
        additional_stops = bool(data.get("additional_stops_planned"))
        combining = bool(data.get("combining_multiple_customers"))
        multiple_bols = bool(data.get("multiple_bols_detected"))
        exclusive = bool(data.get("exclusive_language_detected"))
        appointment = bool(data.get("appointment_constraints_present"))
        is_same_day = bool(data.get("is_same_day"))
        multi_pick = pickups > 1
        multi_drop = deliveries > 1
        consolidation = combining or (multiple_bols and multi_pick)

        reasoning = []
        confidence = "MEDIUM"
        if exclusive:
            classification = "ftl"
            confidence = "HIGH"
            reasoning.append("Exclusive-use language detected")
        elif pickups == 1 and deliveries == 1 and not additional_stops and not consolidation:
            classification = "ftl"
            confidence = "HIGH"
            reasoning.append("Single pickup and delivery without consolidation")
        elif multi_pick or multi_drop or consolidation or combining or additional_stops:
            classification = "ltl"
            confidence = "HIGH"
            reasoning.append("Multi-stop or consolidation pattern detected")
        elif appointment and is_same_day and not additional_stops:
            classification = "ftl"
            confidence = "MEDIUM"
            reasoning.append("Same-day appointment-constrained lane")
        else:
            classification = "ftl"
            confidence = "MEDIUM"
            reasoning.append("Defaulted to FTL due to limited constraints")

        return {
            "classification": classification.upper(),
            "confidence": confidence,
            "reasoning": reasoning,
            "route_structure": {
                "pickup_count": pickups,
                "delivery_count": deliveries,
                "consolidated": consolidation,
                "exclusive_detected": exclusive,
            },
        }

    def _assign_stop_products(self):
        for lead in self:
            stops = lead.dispatch_stop_ids.sorted("sequence")
            if not stops:
                continue

            pickup_count = len(stops.filtered(lambda s: s.stop_type == "pickup"))
            delivery_count = len(stops.filtered(lambda s: s.stop_type == "delivery"))
            classification = lead.classify_load(
                extracted_data={
                    "pickup_locations_count": pickup_count,
                    "delivery_locations_count": delivery_count,
                    "additional_stops_planned": len(stops) > 2,
                }
            )
            is_ftl = classification["classification"] == "FTL"
            lead.ai_classification = "ftl" if is_ftl else "ltl"
            max_capacity = (lead.assigned_vehicle_id.payload_capacity_lbs if lead.assigned_vehicle_id else 40000.0) or 40000.0
            if not is_ftl and sum(stops.mapped("weight_lbs")) > max_capacity:
                raise UserError("LTL consolidation exceeds vehicle payload capacity.")

            for stop in stops:
                stop.is_ftl = bool(is_ftl)
                product = lead._get_service_product()
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
            "billing_mode": self.billing_mode,
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
        pricing_payload = []
        if self.pricing_payload_json:
            pricing_payload = json.loads(self.pricing_payload_json)
        payload_by_stop = {item.get("stop_id"): item for item in pricing_payload}
        base_rate = self.final_rate or self.suggested_rate or 0.0
        stops = self.dispatch_stop_ids.sorted("sequence")
        rate_per_stop = base_rate / len(stops) if stops else 0.0
        for stop in stops:
            product = stop.product_id or stop.freight_product_id
            if not product:
                raise UserError("Each stop requires a service product.")
            payload_line = payload_by_stop.get(stop.id, {})
            segment_rate = payload_line.get("segment_rate", rate_per_stop)
            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product.id,
                    "name": stop.address or product.display_name,
                    "product_uom_qty": 1,
                    "price_unit": max(segment_rate, 0.0),
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

        if not self.pickup_date:
            self.pickup_date = fields.Date.to_date(self._default_pickup_datetime_toronto())
        if not self.delivery_date:
            self.delivery_date = self.pickup_date
        self._assign_stop_products()
        pallet_warning = self._get_pallet_mismatch_warning()
        if pallet_warning:
            self.ai_warning_text = pallet_warning
        self.compute_pricing()
        order = self.env["sale.order"].create(self._prepare_order_values())

        if self.assigned_vehicle_id and self.dispatch_stop_ids and not self.dispatch_run_id:
            from ..services.run_planner_service import RunPlannerService

            planner = RunPlannerService(self.env)
            run_date = self.pickup_date or fields.Date.today()
            run = planner.get_or_create_run(self.assigned_vehicle_id, run_date)
            planner.append_lead_to_run(run, self)
            simulation = planner.simulate_run(run, run.stop_ids.sorted("run_sequence"))
            planner._update_run(run, simulation)
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
