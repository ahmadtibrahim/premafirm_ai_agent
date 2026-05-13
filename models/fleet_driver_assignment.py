from odoo import fields, models


class FleetDriverAssignment(models.Model):
    _name = "fleet.driver.assignment"
    _description = "Driver Assignment History"
    _order = "start_datetime desc"

    vehicle_id = fields.Many2one("fleet.vehicle", required=True, ondelete="cascade", index=True)
    driver_contact_id = fields.Many2one(
        "res.partner",
        string="Driver",
        required=True,
        domain=[("x_is_driver_profile", "=", True)],
        index=True,
    )
    start_datetime = fields.Datetime(required=True)
    end_datetime = fields.Datetime()
    active = fields.Boolean(default=True)
    source = fields.Selection(
        [("manual", "Manual"), ("geotab", "Geotab"), ("system", "System")],
        default="manual",
    )
    notes = fields.Text()
