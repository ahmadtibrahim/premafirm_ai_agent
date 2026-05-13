from odoo import api, fields, models


class FleetTripLog(models.Model):
    _name = "fleet.trip.log"
    _description = "Fleet Trip Log"
    _order = "start_datetime desc"

    vehicle_id = fields.Many2one("fleet.vehicle", required=True, ondelete="cascade", index=True)
    driver_contact_id = fields.Many2one("res.partner", string="Driver", index=True)
    dispatch_id = fields.Many2one("premafirm.dispatch.run", string="Dispatch Run")
    load_id = fields.Many2one("premafirm.load", string="Load")

    start_datetime = fields.Datetime()
    end_datetime = fields.Datetime()

    start_odometer_km = fields.Float(digits=(12, 1))
    end_odometer_km = fields.Float(digits=(12, 1))
    distance_km = fields.Float(
        compute="_compute_distance",
        store=True,
        digits=(8, 1),
    )

    start_engine_hours = fields.Float(digits=(10, 1))
    end_engine_hours = fields.Float(digits=(10, 1))
    engine_hours_used = fields.Float(
        compute="_compute_engine_hours",
        store=True,
        digits=(6, 2),
    )

    start_fuel_percent = fields.Float(digits=(5, 1))
    end_fuel_percent = fields.Float(digits=(5, 1))

    start_location = fields.Char()
    end_location = fields.Char()

    state = fields.Selection(
        [("draft", "Draft"), ("in_progress", "In Progress"), ("done", "Done")],
        default="draft",
    )

    @api.depends("start_odometer_km", "end_odometer_km")
    def _compute_distance(self):
        for rec in self:
            rec.distance_km = max(0.0, (rec.end_odometer_km or 0.0) - (rec.start_odometer_km or 0.0))

    @api.depends("start_engine_hours", "end_engine_hours")
    def _compute_engine_hours(self):
        for rec in self:
            rec.engine_hours_used = max(0.0, (rec.end_engine_hours or 0.0) - (rec.start_engine_hours or 0.0))
