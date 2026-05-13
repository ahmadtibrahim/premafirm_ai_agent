import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PremafirmGeotabDevice(models.Model):
    """Cache of Geotab devices, refreshed on demand from the API."""

    _name = "premafirm.geotab.device"
    _description = "Geotab Device Registry"
    _order = "name"

    geotab_id = fields.Char(string="Geotab Device ID", required=True, index=True)
    name = fields.Char(string="Device Name", required=True)
    vin = fields.Char(string="VIN")
    license_plate = fields.Char(string="License Plate")
    serial_number = fields.Char(string="Serial Number")
    linked_vehicle_id = fields.Many2one(
        "fleet.vehicle",
        string="Linked Odoo Vehicle",
        readonly=True,
    )
    fetched_at = fields.Datetime(string="Last Fetched", readonly=True)

    _sql_constraints = [
        ("geotab_id_unique", "UNIQUE(geotab_id)", "Geotab device ID must be unique.")
    ]

    @api.model
    def refresh_from_geotab(self):
        """Fetch all Geotab devices and update this cache table."""
        from ..services.geotab_service import GeotabService

        cfg = self.env["ir.config_parameter"].sudo()
        if cfg.get_param("geotab.enabled", "False").lower() != "true":
            raise UserError("Geotab integration is not enabled. Enable it in Settings first.")

        try:
            svc = GeotabService(self.env)
            devices = svc.get_devices()
        except Exception as exc:
            raise UserError(f"Could not connect to Geotab: {exc}") from exc

        # Build map of already-linked Odoo vehicles
        linked_map = {
            v.x_geotab_device_id: v.id
            for v in self.env["fleet.vehicle"].sudo().search(
                [("x_geotab_device_id", "!=", False)]
            )
        }

        now = fields.Datetime.now()
        fetched_ids = []

        for device in devices:
            d_id = (device.get("id") or "").strip()
            d_name = (device.get("name") or "").strip()
            if not d_id:
                continue

            vals = {
                "geotab_id": d_id,
                "name": d_name or d_id,
                "vin": (device.get("vehicleIdentificationNumber") or "").strip() or False,
                "license_plate": (device.get("licensePlate") or "").strip() or False,
                "serial_number": (device.get("serialNumber") or "").strip() or False,
                "linked_vehicle_id": linked_map.get(d_id, False),
                "fetched_at": now,
            }
            existing = self.search([("geotab_id", "=", d_id)], limit=1)
            if existing:
                existing.write(vals)
                fetched_ids.append(existing.id)
            else:
                rec = self.create(vals)
                fetched_ids.append(rec.id)

        _logger.info("Geotab device cache refreshed: %d devices", len(fetched_ids))
        return len(fetched_ids)
