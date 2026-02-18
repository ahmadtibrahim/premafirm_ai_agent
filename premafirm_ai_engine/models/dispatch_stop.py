from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PremafirmDispatchStop(models.Model):
    _name = "premafirm.dispatch.stop"
    _description = "Premafirm Dispatch Stop"
    _order = "sequence asc, id asc"

    ALLOWED_SERVICE_TEMPLATES = {
        "FTL Freight Service - Canada",
        "LTL Freight Service - Canada",
        "FTL - Freight Service - USA",
        "LTL - Freight Service - USA",
        "Liftgate",
        "Inside Delivery",
    }

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    sequence = fields.Integer(default=1)

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")], required=True)
    address = fields.Char(required=True)
    address_link_html = fields.Html(compute="_compute_address_link_html", store=True)
    full_address = fields.Char()
    country = fields.Char()
    postal_code = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    place_categories = fields.Char()
    needs_manual_review = fields.Boolean(default=False)
    liftgate_needed = fields.Boolean(default=False)

    pallets = fields.Integer()
    weight_lbs = fields.Float()

    service_type = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")

    requested_datetime = fields.Datetime()
    scheduled_datetime = fields.Datetime("Scheduled Time")
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

    @api.onchange("address")
    def _onchange_address_country(self):
        for stop in self:
            addr = (stop.address or "").upper()
            if any(token in addr for token in [" USA", " US", "UNITED STATES"]):
                stop.country = "USA"
            elif stop.address:
                stop.country = "Canada"

    @api.constrains("product_id")
    def _constrain_allowed_products(self):
        for stop in self:
            if not stop.product_id:
                continue
            tmpl_name = stop.product_id.product_tmpl_id.name
            if tmpl_name not in self.ALLOWED_SERVICE_TEMPLATES:
                raise ValidationError("Service product must be one of the approved freight/accessorial templates.")
