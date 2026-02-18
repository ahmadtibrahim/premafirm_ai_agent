from odoo import fields, models


class PremafirmDispatchRun(models.Model):
    _name = "premafirm.dispatch.run"
    _description = "Premafirm Dispatch Run"

    name = fields.Char(required=True)
    vehicle_id = fields.Many2one("fleet.vehicle", required=True)
    run_date = fields.Date(required=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("planned", "Planned"),
            ("confirmed", "Confirmed"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
    )
    start_datetime = fields.Datetime()
    end_datetime = fields.Datetime()
    calendar_event_id = fields.Many2one("calendar.event")
    total_distance_km = fields.Float()
    total_drive_hours = fields.Float()
    empty_distance_km = fields.Float()
    loaded_distance_km = fields.Float()
    estimated_revenue = fields.Monetary(currency_field="currency_id")
    estimated_cost = fields.Monetary(currency_field="currency_id")
    estimated_profit = fields.Monetary(currency_field="currency_id")
    notes = fields.Text()

    currency_id = fields.Many2one("res.currency", related="vehicle_id.company_id.currency_id", store=True, readonly=True)
    stop_ids = fields.One2many("premafirm.dispatch.stop", "run_id")
