from odoo import fields, models


class FleetVehicleOdometer(models.Model):
    _inherit = "fleet.vehicle.odometer"

    engine_hours = fields.Float(
        string="Engine Hours",
        digits=(10, 1),
        default=0.0,
    )
