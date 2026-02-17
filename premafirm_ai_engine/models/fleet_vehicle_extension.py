from odoo import fields, models


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    vehicle_work_start_time = fields.Float(
        default=8.0,
        help="Work start time in 24-hour format (e.g., 8.5 = 08:30).",
    )
