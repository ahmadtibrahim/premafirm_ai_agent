import json
import re
from datetime import timedelta

from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import UserError

try:
    from ..services.dispatch_rules_engine import DispatchRulesEngine
except Exception:
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    _module_path = Path(__file__).resolve().parents[1] / "services" / "dispatch_rules_engine.py"
    _spec = spec_from_file_location("dispatch_rules_engine", _module_path)
    _module = module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    DispatchRulesEngine = _module.DispatchRulesEngine


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
    ai_override_command = fields.Text()
    ai_locked = fields.Boolean(default=False)
    load_status = fields.Selection(
        [
            ("draft", "Draft"),
            ("quoted", "Quoted"),
            ("approved", "Approved"),
            ("dispatched", "Dispatched"),
            ("at_pickup", "At Pickup"),
            ("loaded", "Loaded"),
            ("in_transit", "In Transit"),
            ("at_delivery", "At Delivery"),
            ("completed", "Completed"),
            ("invoiced", "Invoiced"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        tracking=True,
    )
    vehicle_booking_ids = fields.One2many("premafirm.booking", "lead_id", string="Vehicle Bookings")
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

    recommended_schedule = fields.Text()
    booking_hos_status = fields.Text()
    weather_risk = fields.Selection([("low", "LOW"), ("moderate", "MODERATE"), ("high", "HIGH"), ("severe", "SEVERE")])
    profit_estimate = fields.Float()
    selected_service_product_id = fields.Integer()
    selected_accessorial_product_ids = fields.Char()


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

    def _extract_city(self, address):
        return (address.split(",", 1)[0] or "").strip() if address else ""

    def _is_us_stop(self, stop):
        country = (stop.country or "").upper()
        if country:
            return country in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
        address = (stop.address or "").upper()
        return "USA" in address or "UNITED STATES" in address or ", US" in address

    def get_home_base(self):
        self.ensure_one()
        company = self.env.company
        return company.partner_id

    def _get_leave_yard_field_name(self):
        self.ensure_one()
        if "x_leave_yard_at" in self._fields:
            return "x_leave_yard_at"
        if "leave_yard_at" in self._fields:
            return "leave_yard_at"
        return False

    def _resolve_equipment(self):
        self.ensure_one()
        if self.reefer_required:
            return "Reefer"
        return "Dry"

    def _resolve_structure(self):
        self.ensure_one()
        multiple_bol = bool(self.bol_number and "," in self.bol_number)
        if multiple_bol:
            return "LTL"
        if self.billing_mode == "per_stop":
            return "LTL"
        pallets = int(self.total_pallets or 0)
        delivery_stops = len(self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery"))
        dedicated_truck = bool(self.assigned_vehicle_id)
        if pallets >= 8 and dedicated_truck:
            return "FTL"
        if pallets < 8 and delivery_stops > 1:
            return "LTL"
        return "FTL"

    def _get_service_product_id(self):
        self.ensure_one()
        rules = DispatchRulesEngine(self.env)
        customer_country = (self.partner_id.country_id.name or "") if self.partner_id and self.partner_id.country_id else ""
        return rules.select_product(customer_country, self._resolve_structure(), self._resolve_equipment())

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
                f"• Structure: {lead._resolve_structure()} | Equipment: {lead._resolve_equipment().upper()}",
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
                vals["reefer_required"] = True
            elif "dry" in lowered:
                vals["equipment_type"] = "dry"
                vals["reefer_required"] = False

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
            lead.dispatch_stop_ids.unlink()
            lead.write(
                {
                    "billing_mode": "flat",
                    "final_rate": 0.0,
                    "ai_override_command": False,
                    "ai_internal_summary": False,
                    "ai_customer_email": False,
                    "ai_recommendation": False,
                    "ai_optimization_suggestion": False,
                    "ai_warning_text": False,
                    "pricing_payload_json": False,
                    "suggested_rate": False,
                    "estimated_cost": False,
                    "load_status": "draft",
                }
            )
        return True


    def action_unlock_ai(self):
        self.write({"ai_locked": False})

    def action_mark_quoted(self):
        self.write({"load_status": "quoted"})

    @api.constrains("final_rate")
    def _check_non_negative_final_rate(self):
        for lead in self:
            if (lead.final_rate or 0.0) < 0.0:
                raise UserError("Final rate cannot be negative.")

    def _default_pickup_datetime_company_tz(self):
        tz_name = self.env.user.tz or self.env.company.partner_id.tz or "UTC"
        tz = ZoneInfo(tz_name)
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
        multiple_bol = bool(data.get("multiple_bol", False))
        rate_type = data.get("rate_type") or self.billing_mode
        pallet_count = int(data.get("pallet_count") or self.total_pallets or 0)
        inferred_stops = int(data.get("delivery_locations_count") or 0)
        if not inferred_stops and getattr(self, "dispatch_stop_ids", None):
            inferred_stops = len(self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery"))
        number_of_stops = int(data.get("number_of_stops") or inferred_stops)
        dedicated_truck = bool(data.get("dedicated_truck", self.assigned_vehicle_id))
        if data.get("additional_stops_planned"):
            return {"classification": "LTL", "confidence": "HIGH"}

        if multiple_bol is True:
            classification = "LTL"
        elif rate_type == "per_stop":
            classification = "LTL"
        elif pallet_count >= 8 and dedicated_truck is True:
            classification = "FTL"
        elif pallet_count < 8 and number_of_stops > 1:
            classification = "LTL"
        else:
            classification = "FTL"

        return {"classification": classification, "confidence": "HIGH" if classification == "LTL" else "MEDIUM"}

    def _assign_stop_products(self):
        for lead in self:
            stops = lead.dispatch_stop_ids.sorted("sequence")
            if not stops:
                continue
            product_id = lead._get_service_product_id()
            structure = lead._resolve_structure()
            lead.ai_classification = "ftl" if structure == "FTL" else "ltl"
            for stop in stops:
                stop.is_ftl = structure == "FTL"
                stop.product_id = product_id
                stop.freight_product_id = product_id
            lead.product_id = product_id

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
        self.ensure_one()
        product_id = self._get_service_product_id()
        delivery_stops = self.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")
        load_count = max(int(max(delivery_stops.mapped("load_number"), default=0)), 1)
        total_rate = max(self.final_rate or 0.0, 0.0)
        per_load_rate = round(total_rate / load_count, 2) if load_count else total_rate

        for load_idx in range(1, load_count + 1):
            price_unit = per_load_rate
            if load_idx == load_count:
                price_unit = round(total_rate - (per_load_rate * (load_count - 1)), 2)
            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product_id,
                    "name": f"Load #{load_idx}",
                    "product_uom_qty": 1,
                    "price_unit": price_unit,
                }
            )

        for accessorial_id in DispatchRulesEngine(self.env).accessorial_product_ids(self.liftgate, self.inside_delivery):
            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": accessorial_id,
                    "product_uom_qty": 1,
                    "price_unit": 0.0,
                }
            )

    def action_create_sales_order(self):
        self.ensure_one()

        if not self.partner_id:
            raise UserError("A customer must be selected before creating a sales order.")
        if not self.dispatch_stop_ids:
            raise UserError("Add dispatch stops before creating a sales order.")

        if not self.pickup_date:
            self.pickup_date = fields.Date.to_date(self._default_pickup_datetime_company_tz())
        if not self.delivery_date:
            self.delivery_date = self.pickup_date
        self._assign_stop_products()
        self.selected_service_product_id = self._get_service_product_id()
        self.selected_accessorial_product_ids = ",".join(str(x) for x in DispatchRulesEngine(self.env).accessorial_product_ids(self.liftgate, self.inside_delivery))
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
