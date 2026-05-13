import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class FleetDailyOdometer(models.Model):
    _name = "fleet.daily.odometer"
    _description = "Fleet Daily Odometer Log"
    _order = "log_date desc, vehicle_id"
    _rec_name = "log_date"

    vehicle_id = fields.Many2one(
        "fleet.vehicle",
        required=True,
        ondelete="cascade",
        index=True,
        string="Vehicle",
    )
    log_date = fields.Date(required=True, index=True, string="Date")
    odometer_km = fields.Float(string="Odometer (km)", digits=(12, 1))
    engine_hours = fields.Float(string="Engine Hours", digits=(10, 1))
    driver_contact_id = fields.Many2one(
        "res.partner",
        string="Driver",
        domain=[("x_is_driver_profile", "=", True)],
    )
    source = fields.Selection(
        [("manual", "Manual"), ("geotab", "Geotab")],
        default="geotab",
        string="Source",
    )
    notes = fields.Char(string="Notes")

    _sql_constraints = [
        (
            "unique_vehicle_date",
            "UNIQUE(vehicle_id, log_date)",
            "Only one daily odometer entry per vehicle per day.",
        ),
    ]

    # ------------------------------------------------------------------
    # Sync to fleet.vehicle.odometer on every create / write
    # ------------------------------------------------------------------

    def _sync_to_fleet_odometer(self):
        FleetOdo = self.env["fleet.vehicle.odometer"].sudo()
        for rec in self:
            if not rec.vehicle_id or not rec.odometer_km:
                continue
            existing = FleetOdo.search([
                ("vehicle_id", "=", rec.vehicle_id.id),
                ("date", "=", rec.log_date),
            ], limit=1)
            vals = {
                "vehicle_id": rec.vehicle_id.id,
                "value": rec.odometer_km,
                "date": rec.log_date,
                "engine_hours": rec.engine_hours or 0.0,
            }
            try:
                if existing:
                    existing.write(vals)
                else:
                    FleetOdo.create(vals)
            except Exception as exc:
                _logger.warning("Failed to sync daily odometer to fleet.vehicle.odometer for %s on %s: %s",
                                rec.vehicle_id.name, rec.log_date, exc)

    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_to_fleet_odometer()
        return records

    def write(self, vals):
        res = super().write(vals)
        self._sync_to_fleet_odometer()
        return res
