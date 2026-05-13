import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class FleetGeotabLinkWizard(models.TransientModel):
    """Wizard to select a Geotab device, link it to an Odoo vehicle, and import all data."""

    _name = "fleet.geotab.link.wizard"
    _description = "Link Vehicle to Geotab Device"

    vehicle_id = fields.Many2one(
        "fleet.vehicle",
        string="Odoo Vehicle",
        required=True,
        readonly=True,
    )
    # Domain allows: unlinked devices OR the device already linked to THIS vehicle
    geotab_device_id = fields.Many2one(
        "premafirm.geotab.device",
        string="Geotab Device",
        domain="['|', ('linked_vehicle_id', '=', False), ('linked_vehicle_id', '=', vehicle_id)]",
    )

    # Informational display fields
    device_vin = fields.Char(related="geotab_device_id.vin", string="VIN", readonly=True)
    device_plate = fields.Char(
        related="geotab_device_id.license_plate", string="License Plate", readonly=True
    )
    device_serial = fields.Char(
        related="geotab_device_id.serial_number", string="Serial #", readonly=True
    )
    device_count = fields.Integer(compute="_compute_device_count", string="Available Devices")
    last_fetched = fields.Datetime(compute="_compute_last_fetched", string="Cache Last Updated")

    @api.depends("vehicle_id")
    def _compute_device_count(self):
        for rec in self:
            rec.device_count = self.env["premafirm.geotab.device"].search_count(
                ["|", ("linked_vehicle_id", "=", False),
                      ("linked_vehicle_id", "=", rec.vehicle_id.id)]
            )

    def _compute_last_fetched(self):
        latest = self.env["premafirm.geotab.device"].search([], order="fetched_at desc", limit=1)
        for rec in self:
            rec.last_fetched = latest.fetched_at if latest else False

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        vehicle_id = self.env.context.get("default_vehicle_id")
        if vehicle_id and "geotab_device_id" in fields_list:
            # Pre-select the currently linked device if any
            vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
            if vehicle.x_geotab_device_id:
                device = self.env["premafirm.geotab.device"].search(
                    [("geotab_id", "=", vehicle.x_geotab_device_id)], limit=1
                )
                if device:
                    res["geotab_device_id"] = device.id
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh_devices(self):
        """Pull latest device list from Geotab and reload the wizard."""
        self.env["premafirm.geotab.device"].refresh_from_geotab()
        return {
            "type": "ir.actions.act_window",
            "name": "Link to Geotab Device",
            "res_model": "fleet.geotab.link.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": self.env.context,
        }

    def action_link_and_import(self):
        """Link the selected Geotab device to this vehicle and import all available data."""
        self.ensure_one()
        if not self.geotab_device_id:
            raise UserError("Please select a Geotab device to link.")

        device = self.geotab_device_id
        vehicle = self.vehicle_id

        # If vehicle was linked to a different device before, release that old link
        if vehicle.x_geotab_device_id and vehicle.x_geotab_device_id != device.geotab_id:
            old_device = self.env["premafirm.geotab.device"].search(
                [("geotab_id", "=", vehicle.x_geotab_device_id)], limit=1
            )
            if old_device:
                old_device.write({"linked_vehicle_id": False})

        # Write the new link
        write_vals = {
            "x_geotab_device_id": device.geotab_id,
            "x_geotab_vehicle_id": device.serial_number or device.geotab_id,
            "x_external_source": "geotab",
            "x_sync_status": "pending",
            "x_sync_error": False,
        }
        # Populate once-if-empty fields from Geotab device
        if device.vin and not vehicle.vin_sn:
            write_vals["vin_sn"] = device.vin
        if device.license_plate and not vehicle.license_plate:
            write_vals["license_plate"] = device.license_plate

        vehicle.write(write_vals)

        # Mark device as linked to this vehicle
        device.write({"linked_vehicle_id": vehicle.id})

        # Import all live data immediately
        try:
            self.env["premafirm.geotab.sync"].import_vehicle_data(vehicle)
            msg = f"Vehicle linked to '{device.name}' and live data imported successfully."
        except Exception as exc:
            _logger.warning("Geotab import after link failed for %s: %s", vehicle.name, exc)
            vehicle.write({"x_sync_status": "error", "x_sync_error": str(exc)[:500]})
            msg = f"Vehicle linked to '{device.name}' but live data import failed: {exc}"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Geotab Linked",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }
