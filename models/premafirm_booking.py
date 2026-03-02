from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PremafirmBooking(models.Model):
    _name = "premafirm.booking"
    _description = "Vehicle Booking"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "start_datetime"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade", tracking=True)
    vehicle_id = fields.Many2one("fleet.vehicle", required=True, tracking=True)
    driver_id = fields.Many2one("hr.employee", tracking=True)
    start_datetime = fields.Datetime(required=True, tracking=True)
    end_datetime = fields.Datetime(required=True, tracking=True)
    duration_hours = fields.Float(compute="_compute_duration_hours", store=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )

    vehicle_model_snapshot = fields.Char(readonly=True)
    license_plate_snapshot = fields.Char(readonly=True)
    unit_number_snapshot = fields.Char(readonly=True)
    driver_name_snapshot = fields.Char(readonly=True)

    @api.depends("start_datetime", "end_datetime")
    def _compute_duration_hours(self):
        for rec in self:
            if rec.start_datetime and rec.end_datetime:
                rec.duration_hours = max((rec.end_datetime - rec.start_datetime).total_seconds() / 3600.0, 0.0)
            else:
                rec.duration_hours = 0.0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("lead_id") and vals.get("start_datetime") and not vals.get("end_datetime"):
                vals["end_datetime"] = fields.Datetime.to_datetime(vals["start_datetime"]) + timedelta(hours=6)
        return super().create(vals_list)

    @api.onchange("start_datetime")
    def _onchange_start_datetime(self):
        if self.lead_id and self.start_datetime and not self.end_datetime:
            self.end_datetime = self.start_datetime + timedelta(hours=6)

    @api.constrains("start_datetime", "end_datetime")
    def _check_datetime_order(self):
        for rec in self:
            if rec.start_datetime and rec.end_datetime and rec.end_datetime <= rec.start_datetime:
                raise ValidationError("Booking end time must be after start time.")

    @api.constrains("vehicle_id", "start_datetime", "end_datetime", "state")
    def _check_overlapping_booking(self):
        for rec in self:
            if not rec.vehicle_id or not rec.start_datetime or not rec.end_datetime:
                continue
            overlaps = self.search_count(
                [
                    ("id", "!=", rec.id),
                    ("vehicle_id", "=", rec.vehicle_id.id),
                    ("state", "in", ("draft", "confirmed")),
                    ("start_datetime", "<", rec.end_datetime),
                    ("end_datetime", ">", rec.start_datetime),
                ]
            )
            if overlaps:
                raise ValidationError("The vehicle is already booked during the selected time interval.")

    def action_confirm(self):
        for rec in self:
            vals = {"state": "confirmed"}
            if rec.vehicle_id:
                vals.update(
                    {
                        "vehicle_model_snapshot": rec.vehicle_id.model_id.name,
                        "license_plate_snapshot": rec.vehicle_id.license_plate,
                        "unit_number_snapshot": rec.vehicle_id.name,
                    }
                )
            if rec.driver_id:
                vals["driver_name_snapshot"] = rec.driver_id.name
            rec.write(vals)
            if rec.lead_id and rec.lead_id.load_status in {"draft", "quoted", "approved"}:
                rec.lead_id.load_status = "dispatched"

    def action_done(self):
        self.write({"state": "done"})

    def action_cancel(self):
        self.write({"state": "cancelled"})
