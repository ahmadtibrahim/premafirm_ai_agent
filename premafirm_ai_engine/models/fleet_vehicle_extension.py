from odoo import fields, models

class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    service_type = fields.Selection(
        [
            ("dry", "Dry"),
            ("reefer", "Reefer"),
        ],
        default="dry",
        string="Service Type",
    )

    load_type = fields.Selection(
        [
            ("LTL", "LTL"),
            ("FTL", "FTL"),
        ],
        default="FTL",
        string="Load Type",
    )

    home_location = fields.Char(
        string="Home Location",
    )

    payload_capacity_lbs = fields.Float(default=40000.0)
    driver_id = fields.Many2one("res.partner", domain=[("is_driver", "=", True)])
    home_latitude = fields.Float()
    home_longitude = fields.Float()

    vehicle_work_start_time = fields.Float(
        string="Work Start Hour",
        default=8.0,
    )
