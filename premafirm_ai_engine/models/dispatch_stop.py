from odoo import fields, models


class PremafirmDispatchStop(models.Model):
    _name = "premafirm.dispatch.stop"
    _description = "Premafirm Dispatch Stop"
    _order = "sequence asc, id asc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    sequence = fields.Integer(default=1)

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")], required=True)
    address = fields.Char(required=True)

    pallets = fields.Integer()
    weight_lbs = fields.Float()

    service_type = fields.Selection([("dry", "Dry"), ("reefer", "Reefer")], default="dry")

    requested_datetime = fields.Datetime()
    scheduled_datetime = fields.Datetime("Scheduled Time")
    estimated_arrival = fields.Datetime("ETA")
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
