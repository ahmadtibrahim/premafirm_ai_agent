import json
import re
from datetime import datetime, time, timedelta

from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import UserError

try:
    from ..services.dispatch_rules_engine import DispatchRulesEngine
    from ..services.mapbox_service import MapboxService
except Exception:
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    _module_path = Path(__file__).resolve().parents[1] / "services" / "dispatch_rules_engine.py"
    _spec = spec_from_file_location("dispatch_rules_engine", _module_path)
    _module = module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    DispatchRulesEngine = _module.DispatchRulesEngine
    _map_module_path = Path(__file__).resolve().parents[1] / "services" / "mapbox_service.py"
    _map_spec = spec_from_file_location("mapbox_service", _map_module_path)
    _map_module = module_from_spec(_map_spec)
    _map_spec.loader.exec_module(_map_module)
    MapboxService = _map_module.MapboxService


class CrmLead(models.Model):
    _inherit = "crm.lead"

    DRIVER_PREP_BUFFER = 30.0
    INSPECTION_TIME = 15.0
    ENGINE_WARMUP = 15.0
    LOAD_UNLOAD_TIME = 30.0

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
    final_rate = fields.Monetary(currency_field="company_currency_id", required=True, default=0.0)
    final_rate_total = fields.Monetary(
        currency_field="company_currency_id",
        compute="_compute_final_rate_total",
        store=True,
    )
    ai_recommendation = fields.Text()

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

    leave_yard_at = fields.Datetime("Leave Yard At", compute="_compute_leave_yard_at", store=True)
    departure_time = fields.Datetime(related="leave_yard_at", store=True, readonly=False)

    inside_delivery = fields.Boolean()
    liftgate = fields.Boolean()
    detention_requested = fields.Boolean()
    reefer_required = fields.Boolean()
    service_type = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")],
        compute="_compute_equipment_fields",
        store=True,
        readonly=False,
    )
    equipment_type = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")],
        compute="_compute_equipment_fields",
        store=True,
        readonly=False,
    )
    reefer_setpoint_c = fields.Float("Reefer Setpoint (°C)")
    pump_truck_required = fields.Boolean()
    ai_warning_text = fields.Text()
    hos_warning_text = fields.Char(compute="_compute_hos_warning_text", store=True)

    assigned_vehicle_id = fields.Many2one("fleet.vehicle")

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
    strict_pickup_start = fields.Datetime()
    strict_pickup_end = fields.Datetime()
    strict_delivery_start = fields.Datetime()
    strict_delivery_end = fields.Datetime()
    notify_driver = fields.Boolean(default=False)
    schedule_api_warning = fields.Text(readonly=True)
    ai_optimization_suggestion = fields.Text()

    recommended_schedule = fields.Text()
    booking_hos_status = fields.Text()
    weather_risk = fields.Selection([("low", "LOW"), ("moderate", "MODERATE"), ("high", "HIGH"), ("severe", "SEVERE")])
    weather_summary = fields.Text()
    weather_temp_c = fields.Float()
    precip_type = fields.Selection([("none", "None"), ("rain", "Rain"), ("snow", "Snow"), ("sleet", "Sleet"), ("mixed", "Mixed")], default="none")
    precip_prob = fields.Float()
    wind_kph = fields.Float()
    weather_alert_level = fields.Selection([("none", "None"), ("info", "Info"), ("warn", "Warn"), ("severe", "Severe")], default="none")
    weather_alert_text = fields.Text()
    weather_checked_at = fields.Datetime()
    profit_estimate = fields.Float()
    selected_service_product_id = fields.Integer()
    selected_accessorial_product_ids = fields.Char()

    @api.depends("dispatch_stop_ids.scheduled_datetime", "dispatch_stop_ids.drive_minutes", "assigned_vehicle_id", "assigned_vehicle_id.work_start_hour")
    def _compute_leave_yard_at(self):
        for lead in self:
            first_stop = lead.dispatch_stop_ids.sorted("sequence")[:1]
            if first_stop and first_stop.scheduled_datetime:
                lead.leave_yard_at = first_stop.scheduled_datetime - timedelta(minutes=float(first_stop.drive_minutes or 0.0))
            else:
                lead.leave_yard_at = False

    @api.depends("reefer_required")
    def _compute_equipment_fields(self):
        for lead in self:
            value = "reefer" if lead.reefer_required else "dry"
            lead.service_type = value
            lead.equipment_type = value

    def _vehicle_start_datetime(self):
        self.ensure_one()
        tz_name = self.env.company.partner_id.tz or "America/Toronto"
        tz = ZoneInfo(tz_name)
        now_utc = fields.Datetime.now()
        if getattr(now_utc, "tzinfo", None) is None:
            now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
        now_local = now_utc.astimezone(tz)

        hour_float = float((self.assigned_vehicle_id.work_start_hour if self.assigned_vehicle_id else 8.0) or 8.0)
        hh = int(hour_float)
        mm = int(round((hour_float - hh) * 60))

        start_date = now_local.date()
        if now_local.hour >= 13:
            start_date = start_date + timedelta(days=1)
        candidate_local = datetime.combine(start_date, time(hh, mm), tzinfo=tz)

        if 8 <= now_local.hour < 13:
            same_day_floor = datetime.combine(now_local.date(), time(hh, mm), tzinfo=tz)
            candidate_local = max(now_local, same_day_floor)
        elif now_local.hour < 8:
            candidate_local = datetime.combine(now_local.date(), time(hh, mm), tzinfo=tz)

        return candidate_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def _stop_window(self, stop):
        start = stop.time_window_start or (stop.pickup_window_start if stop.stop_type == "pickup" else stop.delivery_window_start)
        end = stop.time_window_end or (stop.pickup_window_end if stop.stop_type == "pickup" else stop.delivery_window_end)
        if stop.stop_type == "pickup":
            start = self.strict_pickup_start or start
            end = self.strict_pickup_end or end
        else:
            start = self.strict_delivery_start or start
            end = self.strict_delivery_end or end
        return start, end

    def _compute_schedule(self, manual_stop=False):
        mapbox = MapboxService(self.env)
        for lead in self:
            if lead.schedule_locked:
                continue
            ordered = lead.dispatch_stop_ids.sorted("sequence")
            if not ordered:
                lead.write({"leave_yard_at": False, "schedule_conflict": False, "schedule_api_warning": False})
                continue
            vehicle = lead.assigned_vehicle_id
            yard_location = (vehicle.home_location if vehicle else False) or (ordered and ordered[0].home_location) or False
            vehicle_start = lead._vehicle_start_datetime()
            has_window = any(bool(lead._stop_window(stop)[0]) for stop in ordered)
            warnings = []
            conflict = False

            total_weight = sum(ordered.mapped("weight_lbs"))
            total_pallets = sum(ordered.mapped("pallets"))
            payload_limit = float((getattr(vehicle, "max_weight", 0.0) or vehicle.payload_capacity_lbs or 0.0)) if vehicle else 0.0
            pallet_limit = float(getattr(vehicle, "max_pallets", 0.0) or 0.0) if vehicle else 0.0
            if payload_limit and total_weight > payload_limit:
                raise UserError(f"Vehicle capacity exceeded: weight {total_weight:.0f} lbs exceeds limit {payload_limit:.0f} lbs.")
            if pallet_limit and total_pallets > pallet_limit:
                raise UserError(f"Vehicle capacity exceeded: pallets {total_pallets:.0f} exceeds limit {pallet_limit:.0f}.")

            segment_data = []
            prev_loc = yard_location
            for stop in ordered:
                stop_location = stop.full_address or stop.address
                travel = mapbox.get_travel_time(prev_loc, stop_location)
                fallback_minutes = float(stop.drive_minutes or stop.drive_hours * 60.0 or 0.0)
                drive_minutes = float(travel.get("drive_minutes") or fallback_minutes)
                distance_km = float(travel.get("distance_km") or stop.distance_km or 0.0)
                adjusted_minutes = drive_minutes
                if travel.get("warning"):
                    warnings.append(travel.get("warning"))
                segment_data.append({
                    "stop": stop,
                    "distance_km": distance_km,
                    "base_drive_minutes": drive_minutes,
                    "drive_minutes": adjusted_minutes,
                    "map_url": travel.get("map_url"),
                })
                prev_loc = stop_location

            updates = {}
            if manual_stop:
                manual_idx = next((idx for idx, seg in enumerate(segment_data) if seg["stop"].id == manual_stop.id), 0)
                current_time = manual_stop.estimated_arrival or manual_stop.scheduled_datetime or (vehicle_start + timedelta(minutes=(lead.DRIVER_PREP_BUFFER + lead.INSPECTION_TIME + lead.ENGINE_WARMUP)))
                current_time = current_time + timedelta(minutes=float(manual_stop.service_duration or 30.0))
                for idx in range(manual_idx + 1, len(segment_data)):
                    seg = segment_data[idx]
                    eta = max(current_time + timedelta(minutes=seg["drive_minutes"]), vehicle_start)
                    start, end = lead._stop_window(seg["stop"])
                    if start and eta < start:
                        eta = start
                    if end and eta > end:
                        if (seg["stop"].stop_type == "pickup" and (lead.strict_pickup_start or lead.strict_pickup_end)) or (
                            seg["stop"].stop_type == "delivery" and (lead.strict_delivery_start or lead.strict_delivery_end)
                        ):
                            raise UserError("Pickup/Delivery window impossible within vehicle constraints.")
                        conflict = True
                    updates[seg["stop"].id] = {
                        "estimated_arrival": eta,
                        "scheduled_datetime": eta,
                        "scheduled_start_datetime": eta,
                        "scheduled_end_datetime": eta + timedelta(minutes=float(seg["stop"].service_duration or 30.0)),
                        "distance_km": seg["distance_km"],
                        "drive_minutes": seg["drive_minutes"],
                        "map_url": seg["map_url"],
                        "auto_scheduled": True,
                    }
                    current_time = eta + timedelta(minutes=float(seg["stop"].service_duration or 30.0))
                lead_leave_yard = lead.leave_yard_at or vehicle_start
            elif has_window:
                first_window_idx = next((idx for idx, seg in enumerate(segment_data) if lead._stop_window(seg["stop"])[0]), 0)
                arrival_times = {}
                target = lead._stop_window(segment_data[first_window_idx]["stop"])[0]
                arrival_times[first_window_idx] = target
                for idx in range(first_window_idx, -1, -1):
                    seg = segment_data[idx]
                    arrival = arrival_times[idx]
                    depart_prev = arrival - timedelta(minutes=seg["drive_minutes"])
                    if idx == 0:
                        lead_leave_yard = depart_prev
                    else:
                        prev_service = float(segment_data[idx - 1]["stop"].service_duration or 30.0)
                        arrival_times[idx - 1] = depart_prev - timedelta(minutes=prev_service)
                if lead_leave_yard < vehicle_start:
                    conflict = True
                    lead_leave_yard = vehicle_start
                current_time = lead_leave_yard
                for idx, seg in enumerate(segment_data):
                    eta = arrival_times.get(idx) or (current_time + timedelta(minutes=seg["drive_minutes"]))
                    start, end = lead._stop_window(seg["stop"])
                    if start and eta < start:
                        eta = start
                    if end and eta > end:
                        if (seg["stop"].stop_type == "pickup" and (lead.strict_pickup_start or lead.strict_pickup_end)) or (
                            seg["stop"].stop_type == "delivery" and (lead.strict_delivery_start or lead.strict_delivery_end)
                        ):
                            raise UserError("Pickup/Delivery window impossible within vehicle constraints.")
                        conflict = True
                    service = float(seg["stop"].service_duration or 30.0)
                    updates[seg["stop"].id] = {
                        "estimated_arrival": eta,
                        "scheduled_datetime": eta,
                        "scheduled_start_datetime": eta,
                        "scheduled_end_datetime": eta + timedelta(minutes=service),
                        "distance_km": seg["distance_km"],
                        "drive_minutes": seg["drive_minutes"],
                        "map_url": seg["map_url"],
                        "auto_scheduled": True,
                    }
                    current_time = eta + timedelta(minutes=service)
            else:
                lead_leave_yard = vehicle_start
                current_time = vehicle_start + timedelta(minutes=(lead.DRIVER_PREP_BUFFER + lead.INSPECTION_TIME + lead.ENGINE_WARMUP))
                for seg in segment_data:
                    eta = max(current_time + timedelta(minutes=seg["drive_minutes"]), vehicle_start)
                    service = float(seg["stop"].service_duration or 30.0)
                    updates[seg["stop"].id] = {
                        "estimated_arrival": eta,
                        "scheduled_datetime": eta,
                        "scheduled_start_datetime": eta,
                        "scheduled_end_datetime": eta + timedelta(minutes=service),
                        "distance_km": seg["distance_km"],
                        "drive_minutes": seg["drive_minutes"],
                        "map_url": seg["map_url"],
                        "auto_scheduled": True,
                    }
                    current_time = eta + timedelta(minutes=service)

            prev_end = False
            for stop in ordered:
                vals = updates.get(stop.id)
                if vals and not vals.get("scheduled_datetime"):
                    vals["scheduled_datetime"] = vehicle_start
                    vals["scheduled_start_datetime"] = vals.get("scheduled_start_datetime") or vehicle_start
                    vals["scheduled_end_datetime"] = vals.get("scheduled_end_datetime") or (
                        vehicle_start + timedelta(minutes=float(stop.service_duration or 30.0))
                    )
                if vals:
                    stop.with_context(skip_schedule_recompute=True).write(vals)
                start_dt = (vals or {}).get("scheduled_start_datetime") or stop.scheduled_start_datetime
                end_dt = (vals or {}).get("scheduled_end_datetime") or stop.scheduled_end_datetime
                if prev_end and start_dt and start_dt < prev_end:
                    conflict = True
                prev_end = end_dt or prev_end
            lead.write({
                "leave_yard_at": lead_leave_yard,
                "schedule_conflict": conflict or bool(lead_leave_yard and lead_leave_yard < vehicle_start),
                "schedule_api_warning": " | ".join(dict.fromkeys([w for w in warnings if w])) if warnings else False,
            })




    def write(self, vals):
        pricing_fields = {"final_rate", "suggested_rate", "estimated_cost"}
        if pricing_fields.intersection(vals) and not (
            self.env.user.has_group("sales_team.group_sale_manager") or self.env.user.has_group("base.group_system")
        ):
            raise UserError("Only Sales Managers or Administrators can modify pricing fields.")
        res = super().write(vals)
        schedule_triggers = {
            "assigned_vehicle_id",
            "weather_alert_level",
            "weather_checked_at",
        }
        if not self.env.context.get("skip_schedule_recompute") and any(k in vals for k in schedule_triggers):
            self.with_context(skip_schedule_recompute=True)._compute_schedule()
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

    @api.depends("final_rate", "suggested_rate")
    def _compute_final_rate_total(self):
        """Keep pricing total centralized and independent from legacy pricing adjustments."""
        for lead in self:
            lead.final_rate_total = max(lead.final_rate or lead.suggested_rate or 0.0, 0.0)

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
                "distance_km": self.total_distance_km,
                "pallets": self.total_pallets,
                "final_rate": self.final_rate,
                "user_modified": user_modified,
                "timestamp": fields.Datetime.now(),
            }
        )

    def compute_pricing(self):
        for lead in self:
            if (lead.final_rate or 0.0) <= 0.0:
                raise UserError("Final rate must be greater than zero.")

            stops = lead.dispatch_stop_ids.sorted("sequence")
            rate = max(lead.final_rate or lead.suggested_rate or 0.0, 0.0)
            if rate < 0.0:
                raise UserError("Pricing cannot be negative.")

            payload = []
            delivery_stops = stops.filtered(lambda s: s.stop_type == "delivery")
            total_km = sum(max(stop.distance_km or 0.0, 0.0) for stop in delivery_stops) or lead.total_distance_km or 0.0
            # flat mode assumed by default
            distance_rate = (rate / total_km) if total_km else 0.0

            flat_delivery_ids = [stop.id for stop in delivery_stops]
            for stop in stops:
                segment_km = max(stop.distance_km or 0.0, 0.0)
                pallets = max(stop.pallets or 0, 0)
                if stop.id in flat_delivery_ids:
                    segment_rate = segment_km * distance_rate
                else:
                    segment_rate = 0.0
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

            if flat_delivery_ids:
                delivery_payload = [item for item in payload if item["stop_type"] == "delivery"]
                rounded_running = 0.0
                for item in delivery_payload[:-1]:
                    item["segment_rate"] = round(item["segment_rate"], 2)
                    rounded_running += item["segment_rate"]
                delivery_payload[-1]["segment_rate"] = round(max(rate - rounded_running, 0.0), 2)

            bullets = [
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
                    "ai_customer_email": f"Quoted total {total_segment_amount:.2f}.",
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
            lead.dispatch_stop_ids.sudo().unlink()
            lead.write(
                {
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
        self._validate_load_structure()
        self.write({"load_status": "quoted"})

    def _validate_load_structure(self):
        for lead in self:
            stops = lead.dispatch_stop_ids
            unassigned_stops = stops.filtered(lambda s: not s.load_id)
            if unassigned_stops:
                raise UserError("Each load must have exactly one pickup and one delivery before quoting or creating Sales Orders.")

            for load in stops.mapped("load_id"):
                load_stops = stops.filtered(lambda s: s.load_id == load)
                pickups = len(load_stops.filtered(lambda s: s.stop_type == "pickup"))
                deliveries = len(load_stops.filtered(lambda s: s.stop_type == "delivery"))
                if pickups != 1 or deliveries != 1:
                    raise UserError("Each load must have exactly one pickup and one delivery before quoting or creating Sales Orders.")

    def action_rebuild_loads_from_ai(self):
        for lead in self:
            lead.dispatch_stop_ids.write({"load_id": False})
            loads = self.env["premafirm.load"].search([("lead_id", "=", lead.id)])
            loads.sudo().unlink()
            grouped_loads = {}
            current_load = self.env["premafirm.load"]
            for stop in lead.dispatch_stop_ids.sorted("sequence"):
                section_key = stop.load_key or (stop.extracted_load_name or "").strip().lower() or False
                if section_key:
                    if section_key not in grouped_loads:
                        vals = {"lead_id": lead.id}
                        if stop.extracted_load_name:
                            vals["name"] = stop.extracted_load_name
                        grouped_loads[section_key] = self.env["premafirm.load"].create(vals)
                    stop.load_id = grouped_loads[section_key]
                    continue

                if stop.stop_type == "pickup" or not current_load:
                    vals = {"lead_id": lead.id}
                    if stop.extracted_load_name:
                        vals["name"] = stop.extracted_load_name
                    current_load = self.env["premafirm.load"].create(vals)
                stop.load_id = current_load
        return True

    @api.constrains("final_rate")
    def _check_non_negative_final_rate(self):
        for lead in self:
            if (lead.final_rate or 0.0) < 0.0:
                raise UserError("Final rate cannot be negative.")

    def _default_pickup_datetime_company_tz(self):
        tz_name = self.env.company.partner_id.tz or "America/Toronto"
        tz = ZoneInfo(tz_name)
        now_utc = fields.Datetime.now()
        if getattr(now_utc, "tzinfo", None) is None:
            now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
        now_local = now_utc.astimezone(tz)
        hour_float = float((self.assigned_vehicle_id.work_start_hour if self.assigned_vehicle_id else 8.0) or 8.0)
        hh = int(hour_float)
        mm = int(round((hour_float - hh) * 60))
        if now_local.hour >= 13:
            base_date = now_local.date() + timedelta(days=1)
        else:
            base_date = now_local.date()
        pickup_local = datetime.combine(base_date, time(hh, mm), tzinfo=tz)
        if now_local.hour < 8:
            pickup_local = datetime.combine(now_local.date(), time(hh, mm), tzinfo=tz)
        return pickup_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def classify_load(self, email_text=None, extracted_data=None):
        data = extracted_data or {}
        multiple_bol = bool(data.get("multiple_bol", False))
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
        loads = self.env["premafirm.load"].search([("lead_id", "=", self.id)], order="id asc")
        if not loads:
            return

        total_rate = max(self.final_rate_total or self.final_rate or 0.0, 0.0)
        if total_rate <= 0.0:
            raise UserError("Final rate must be greater than zero before generating quotation lines.")

        if order.currency_id and self.company_currency_id and order.currency_id != self.company_currency_id:
            total_rate = self.company_currency_id._convert(
                total_rate,
                order.currency_id,
                order.company_id,
                fields.Date.context_today(self),
            )

        load_metrics = []
        total_distance = 0.0
        for load in loads:
            load_stops = self.dispatch_stop_ids.filtered(lambda s: s.load_id == load)
            distance_km = max(load.distance_km or 0.0, 0.0)
            pallet_count = sum(max(stop.pallets or 0, 0) for stop in load_stops)
            stop_count = len(load_stops.filtered(lambda s: s.stop_type == "delivery"))
            load_metrics.append((load, distance_km, pallet_count, stop_count, load_stops))
            total_distance += distance_km

        running_total = 0.0
        for idx, (load, distance_km, pallet_count, stop_count, load_stops) in enumerate(load_metrics, start=1):
            line_name = load.name
            if load_stops:
                pickup = load_stops.filtered(lambda s: s.stop_type == "pickup")[:1]
                delivery = load_stops.filtered(lambda s: s.stop_type == "delivery")[:1]
                line_name = f"{load.name} — {self._extract_city(pickup.address)} -> {self._extract_city(delivery.address)}"

            # flat mode assumed by default
            if idx < len(load_metrics):
                ratio = (distance_km / total_distance) if total_distance > 0 else (1.0 / len(load_metrics))
                price_unit = round(total_rate * ratio, 2)
                running_total += price_unit
            else:
                price_unit = round(total_rate - running_total, 2)
            qty = 1.0

            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product_id,
                    "name": line_name,
                    "product_uom_qty": qty,
                    "price_unit": price_unit,
                    "load_id": load.id,
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
        self._validate_load_structure()

        if not self.partner_id:
            raise UserError("A customer must be selected before creating a sales order.")
        if not self.dispatch_stop_ids:
            raise UserError("Add dispatch stops before creating a sales order.")
        if (self.final_rate or 0.0) <= 0.0:
            raise UserError("Final rate is required before creating a quotation.")

        if not self.pickup_date:
            self.pickup_date = fields.Date.to_date(self._default_pickup_datetime_company_tz())
        if not self.delivery_date:
            self.delivery_date = self.pickup_date
        self._assign_stop_products()
        if not self.env["premafirm.load"].search_count([("lead_id", "=", self.id)]):
            self.action_rebuild_loads_from_ai()
        self.selected_service_product_id = self._get_service_product_id()
        self.selected_accessorial_product_ids = ",".join(str(x) for x in DispatchRulesEngine(self.env).accessorial_product_ids(self.liftgate, self.inside_delivery))
        order_vals = self._prepare_order_values()
        order = self.env["sale.order"].search(
            [("opportunity_id", "=", self.id), ("state", "!=", "cancel")],
            order="id desc",
            limit=1,
        )
        if order and order.state in ("draft", "sent"):
            order.write(order_vals)
            order.order_line.unlink()
        elif order and order.state not in ("draft", "sent"):
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.order",
                "res_id": order.id,
                "view_mode": "form",
            }
        else:
            order = self.env["sale.order"].create(order_vals)

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
        if suggestions.get("feasible"):
            run = self.dispatch_run_id
            options = suggestions.get("options") or []
            if options:
                best_option = dict(options[0])
                if suggestions.get("run_id"):
                    best_option["run_id"] = suggestions.get("run_id")
                planner.apply_option(self, best_option)
                run = self.env["premafirm.dispatch.run"].browse(best_option.get("run_id") or suggestions.get("run_id"))
            elif suggestions.get("run_id"):
                run = self.env["premafirm.dispatch.run"].browse(suggestions.get("run_id"))

            if run:
                simulation = planner.simulate_run(run, run.stop_ids.sorted("run_sequence"))
                planner._update_run(run, simulation)
                self.dispatch_run_id = run.id

        self.write({"ai_optimization_suggestion": suggestions.get("text"), "schedule_conflict": not suggestions.get("feasible")})
        return True

    # Backward-compatible button action.
    def action_create_quotation(self):
        return self.action_create_sales_order()
