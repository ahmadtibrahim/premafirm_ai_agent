from odoo import fields, models


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    x_studio_gvore = fields.Float(string="GVW Limit (lbs)", store=True)
    x_studio_height_ft = fields.Float(string="Height (ft)", store=True)
    x_studio_current_load_lbs = fields.Integer(string="Current Load (lbs)", store=True)
    x_studio_x_is_busy = fields.Boolean(string="Is Busy", store=True)
    x_studio_location = fields.Char(string="Current Location", store=True)
    x_studio_service_type = fields.Selection(
        [("reefer", "Reefer"), ("dry", "Dry")],
        string="Default Service Type",
        store=True,
    )
    x_studio_load_type = fields.Selection(
        [("FTL", "FTL"), ("LTL", "LTL")],
        string="Default Load Type",
        store=True,
    )
