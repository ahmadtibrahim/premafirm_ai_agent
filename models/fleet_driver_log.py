from odoo import fields, models


class FleetDriverLog(models.Model):
    _name = "fleet.driver.log"
    _description = "Driver Work Log"
    _order = "log_date desc, start_datetime desc"

    driver_contact_id = fields.Many2one(
        "res.partner",
        string="Driver",
        required=True,
        ondelete="cascade",
        index=True,
    )
    vehicle_id = fields.Many2one("fleet.vehicle", string="Vehicle", index=True)
    source = fields.Selection(
        [("geotab", "Geotab"), ("manual", "Manual")],
        default="geotab",
    )
    log_date = fields.Date(required=True, index=True)
    start_datetime = fields.Datetime()
    end_datetime = fields.Datetime()
    duty_status = fields.Selection(
        [("D", "Driving"), ("ON", "On Duty"), ("OFF", "Off Duty"), ("SB", "Sleeper Berth")],
        string="Duty Status",
    )
    driving_hours = fields.Float(digits=(6, 2))
    on_duty_hours = fields.Float(digits=(6, 2))
    off_duty_hours = fields.Float(digits=(6, 2))
    sleeper_hours = fields.Float(digits=(6, 2))
    idle_hours = fields.Float(digits=(6, 2))
    distance_km = fields.Float(digits=(8, 1))
    engine_hours_used = fields.Float(digits=(6, 2))
    notes = fields.Text()
    raw_external_ref = fields.Char(string="External Ref")
