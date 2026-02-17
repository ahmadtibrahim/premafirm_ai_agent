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

    vehicle_work_start_time = fields.Float(
        string="Work Start Hour",
        default=8.0,
    )
