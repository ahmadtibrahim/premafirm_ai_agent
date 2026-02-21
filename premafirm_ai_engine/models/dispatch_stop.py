from odoo import api, fields, models


class PremafirmDispatchStop(models.Model):
    _name = "premafirm.dispatch.stop"
    _description = "Premafirm Dispatch Stop"
    _order = "sequence asc, id asc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    load_id = fields.Many2one("premafirm.load", index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="cascade")
    sequence = fields.Integer(default=1)
    load_number = fields.Char(compute="_compute_load_number", store=True, readonly=False)
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
    city = fields.Char()
    state = fields.Char()
    country = fields.Char()
    postal_code = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    place_categories = fields.Char()
    needs_manual_review = fields.Boolean(default=False)
    liftgate_needed = fields.Boolean(default=False)

    pallets = fields.Integer()
    weight_lbs = fields.Float()
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

    pickup_window_start = fields.Datetime()
    pickup_window_end = fields.Datetime()
    delivery_window_start = fields.Datetime()
    delivery_window_end = fields.Datetime()

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
    drive_hours = fields.Float()
    map_url = fields.Char()

    run_id = fields.Many2one("premafirm.dispatch.run")
    run_sequence = fields.Integer()
    stop_service_mins = fields.Integer(compute="_compute_stop_service_mins", store=True)
    cargo_delta = fields.Integer(compute="_compute_cargo_delta", store=True)

    is_ftl = fields.Boolean("FTL Stop", default=False)
    product_id = fields.Many2one("product.product", string="Service")
    freight_product_id = fields.Many2one(related="product_id", store=True, readonly=False)

    @api.depends("map_url", "address")
    def _compute_address_link_html(self):
        for stop in self:
            address = stop.address or ""
            if stop.map_url:
                stop.address_link_html = f'<a href="{stop.map_url}" target="_blank">{address}</a>'
            else:
                stop.address_link_html = address

    @api.depends("stop_type")
    def _compute_stop_service_mins(self):
        for stop in self:
            stop.stop_service_mins = 60 if stop.stop_type == "pickup" else 45

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
        records = super().create(vals_list)
        for stop in records:
            if stop.lead_id and not stop.load_id:
                stop._assign_default_load()
        return records

    def write(self, vals):
        old_loads = {stop.id: stop.load_id.id for stop in self}
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
