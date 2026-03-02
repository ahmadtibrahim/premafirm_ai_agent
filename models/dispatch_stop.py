from odoo import api, fields, models


class PremafirmDispatchStop(models.Model):
    _name = "premafirm.dispatch.stop"
    _description = "Premafirm Dispatch Stop"
    _order = "sequence asc, id asc"

    _sql_constraints = [
        ("premafirm_stop_unique_sequence", "unique(lead_id, sequence)", "Duplicate stop sequence is not allowed for the same lead."),
    ]

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    load_id = fields.Many2one("premafirm.load", index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="cascade")
    sequence = fields.Integer(default=1)
    load_number = fields.Char(compute="_compute_load_number", store=True, readonly=False)
    load_key = fields.Char(index=True)
    extracted_load_name = fields.Char()
    name = fields.Char()

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")], required=True)
    pickup_drop = fields.Selection(related="stop_type", store=True, readonly=False)
    delivery_status = fields.Selection(
        [("pending", "Pending"), ("out", "Out for Delivery"), ("delivered", "Delivered")],
        default="pending",
    )
    receiver_signature = fields.Binary()
    receiver_signed_at = fields.Datetime()
    no_signature_approved = fields.Boolean(default=False)
    damage_notes = fields.Text()
    drop_photo = fields.Binary()

    address = fields.Char(required=True)
    address_link_html = fields.Html(compute="_compute_address_link_html", store=True)
    full_address = fields.Char()
    geo_short_address = fields.Char(string="Geo Short Address")
    city = fields.Char()
    state = fields.Char()
    country = fields.Char()
    postal_code = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    place_categories = fields.Char()
    needs_manual_review = fields.Boolean(default=False)
    liftgate_needed = fields.Boolean(default=False)

    pallets = fields.Integer(required=True)
    weight_lbs = fields.Float(required=True)
    weight = fields.Float(related="weight_lbs", store=True, readonly=False)

    service_type = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")

    requested_datetime = fields.Datetime()
    scheduled_datetime = fields.Datetime("Scheduled Time")
    scheduled_start_datetime = fields.Datetime()
    scheduled_end_datetime = fields.Datetime()
    estimated_arrival = fields.Datetime("ETA")
    eta_datetime = fields.Datetime(related="estimated_arrival", store=True, readonly=False)
    pickup_datetime_est = fields.Datetime()
    delivery_datetime_est = fields.Datetime()

    service_duration = fields.Float(default=30.0, help="Service duration at this stop in minutes")
    time_window_start = fields.Datetime()
    time_window_end = fields.Datetime()

    pickup_window_start = fields.Datetime()
    pickup_window_end = fields.Datetime()
    delivery_window_start = fields.Datetime()
    delivery_window_end = fields.Datetime()

    auto_scheduled = fields.Boolean(default=True)

    special_instructions = fields.Char()
    equipment_required = fields.Selection(
        [
            ("dock", "Loading Dock"),
            ("liftgate", "Liftgate Required"),
            ("pallet_jack", "Pallet Jack"),
            ("inside", "Inside Delivery"),
            ("hand", "Hand Unload"),
        ],
        string="Equipment Required",
    )
    cross_dock = fields.Boolean(default=False)

    distance_km = fields.Float()
    drive_minutes = fields.Float()
    drive_hours = fields.Float(compute="_compute_drive_hours", inverse="_inverse_drive_hours", store=True)
    map_url = fields.Char()
    forecast_json = fields.Text()
    home_location = fields.Char(related="lead_id.assigned_vehicle_id.home_location", store=True, readonly=True)

    run_id = fields.Many2one("premafirm.dispatch.run")
    run_sequence = fields.Integer()
    stop_service_mins = fields.Integer(compute="_compute_stop_service_mins", store=True)
    cargo_delta = fields.Integer(compute="_compute_cargo_delta", store=True)

    is_ftl = fields.Boolean("FTL Stop", default=False)
    product_id = fields.Many2one("product.product", string="Service")
    freight_product_id = fields.Many2one(related="product_id", store=True, readonly=False)

    @api.depends("drive_minutes")
    def _compute_drive_hours(self):
        for stop in self:
            stop.drive_hours = (stop.drive_minutes or 0.0) / 60.0

    def _inverse_drive_hours(self):
        for stop in self:
            stop.drive_minutes = (stop.drive_hours or 0.0) * 60.0

    @api.depends("map_url", "address", "full_address", "geo_short_address")
    def _compute_address_link_html(self):
        for stop in self:
            display_address = stop.geo_short_address or stop.full_address or stop.address or ""
            if stop.map_url:
                stop.address_link_html = f'<a href="{stop.map_url}" target="_blank">{display_address}</a>'
            else:
                stop.address_link_html = display_address

    @api.depends("stop_type", "service_duration")
    def _compute_stop_service_mins(self):
        for stop in self:
            stop.stop_service_mins = int(stop.service_duration or (60 if stop.stop_type == "pickup" else 45))

    @api.depends("stop_type")
    def _compute_cargo_delta(self):
        for stop in self:
            stop.cargo_delta = 1 if stop.stop_type == "pickup" else -1

    @api.depends("load_id", "load_id.name")
    def _compute_load_number(self):
        for stop in self:
            stop.load_number = stop.load_id.name if stop.load_id else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("lead_id") and not vals.get("sequence"):
                sibling = self.search([("lead_id", "=", vals["lead_id"])], order="sequence desc", limit=1)
                vals["sequence"] = (sibling.sequence + 1) if sibling else 1
        records = super().create(vals_list)
        for stop in records:
            if stop.lead_id and not stop.load_id:
                stop._assign_default_load()
        leads = records.mapped("lead_id")
        if leads and not self.env.context.get("skip_schedule_recompute"):
            leads.with_context(skip_schedule_recompute=True)._compute_schedule()
        return records

    def write(self, vals):
        old_loads = {stop.id: stop.load_id.id for stop in self}
        manual_eta_change = "estimated_arrival" in vals
        schedule_relevant_fields = {
            "estimated_arrival",
            "scheduled_datetime",
            "service_duration",
            "time_window_start",
            "time_window_end",
            "pickup_window_start",
            "pickup_window_end",
            "delivery_window_start",
            "delivery_window_end",
            "drive_minutes",
            "address",
            "sequence",
            "stop_type",
        }
        should_recompute_schedule = bool(schedule_relevant_fields.intersection(vals))
        result = super().write(vals)
        if "load_id" in vals:
            correction_model = self.env["premafirm.ai.correction"]
            for stop in self:
                old_load_id = old_loads.get(stop.id)
                new_load_id = stop.load_id.id
                if old_load_id != new_load_id:
                    correction_model.create(
                        {
                            "lead_id": stop.lead_id.id,
                            "stop_id": stop.id,
                            "old_load_id": old_load_id,
                            "new_load_id": new_load_id,
                        }
                    )
        if should_recompute_schedule and not self.env.context.get("skip_schedule_recompute"):
            leads = self.mapped("lead_id")
            for lead in leads:
                manual_stop = self.filtered(lambda s: s.lead_id == lead)[:1] if manual_eta_change else False
                lead.with_context(skip_schedule_recompute=True)._compute_schedule(manual_stop=manual_stop)
        return result

    def unlink(self):
        leads = self.mapped("lead_id")
        result = super().unlink()
        if leads and not self.env.context.get("skip_schedule_recompute"):
            leads.with_context(skip_schedule_recompute=True)._compute_schedule()
        return result

    def _assign_default_load(self):
        for stop in self:
            if not stop.lead_id:
                continue
            if stop.load_id:
                continue
            ordered = stop.lead_id.dispatch_stop_ids.sorted(lambda s: (s.sequence, s.id))
            current_load = self.env["premafirm.load"]
            for current_stop in ordered:
                if current_stop.stop_type == "pickup" or not current_load:
                    current_load = self.env["premafirm.load"].create({"lead_id": stop.lead_id.id})
                if not current_stop.load_id:
                    current_stop.load_id = current_load

    @api.onchange("address")
    def _onchange_address_country(self):
        for stop in self:
            addr = (stop.address or "").upper()
            if any(token in addr for token in [" USA", " US", "UNITED STATES"]):
                stop.country = "USA"
            elif stop.address:
                stop.country = "Canada"

    @api.model
    def get_structure_type(self, stops):
        deliveries = stops.filtered(lambda s: s.stop_type == "delivery")
        return "FTL" if len(deliveries) <= 1 else "LTL"
