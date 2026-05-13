import logging
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ContactGeotabLinkWizard(models.TransientModel):
    """Wizard to select a Geotab driver profile and link it to a Driver-tagged Contact."""

    _name = "contact.geotab.link.wizard"
    _description = "Link Driver Contact to Geotab Driver Profile"

    contact_id = fields.Many2one(
        "res.partner",
        string="Driver Contact",
        required=True,
        readonly=True,
    )
    # Domain allows: unlinked drivers OR the driver already linked to THIS contact
    geotab_driver_id = fields.Many2one(
        "premafirm.geotab.driver",
        string="Geotab Driver",
        domain="['|', ('linked_contact_id', '=', False), ('linked_contact_id', '=', contact_id)]",
    )

    # Informational display
    driver_email = fields.Char(related="geotab_driver_id.email", readonly=True)
    driver_employee_no = fields.Char(
        related="geotab_driver_id.employee_number", string="Employee #", readonly=True
    )
    driver_count = fields.Integer(compute="_compute_driver_count", string="Available Drivers")
    last_fetched = fields.Datetime(compute="_compute_last_fetched", string="Cache Last Updated")

    @api.depends("contact_id")
    def _compute_driver_count(self):
        for rec in self:
            rec.driver_count = self.env["premafirm.geotab.driver"].search_count(
                ["|", ("linked_contact_id", "=", False),
                      ("linked_contact_id", "=", rec.contact_id.id)]
            )

    def _compute_last_fetched(self):
        latest = self.env["premafirm.geotab.driver"].search([], order="fetched_at desc", limit=1)
        for rec in self:
            rec.last_fetched = latest.fetched_at if latest else False

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        contact_id = self.env.context.get("default_contact_id")
        if contact_id and "geotab_driver_id" in fields_list:
            # Pre-select already-linked Geotab driver if any
            contact = self.env["res.partner"].browse(contact_id)
            if contact.x_geotab_driver_id:
                driver = self.env["premafirm.geotab.driver"].search(
                    [("geotab_id", "=", contact.x_geotab_driver_id)], limit=1
                )
                if driver:
                    res["geotab_driver_id"] = driver.id
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh_drivers(self):
        """Pull latest driver list from Geotab and reload the wizard."""
        self.env["premafirm.geotab.driver"].refresh_from_geotab()
        return {
            "type": "ir.actions.act_window",
            "name": "Link to Geotab Driver",
            "res_model": "contact.geotab.link.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": self.env.context,
        }

    def action_link_and_import(self):
        """Link the selected Geotab driver to this contact and import available data."""
        self.ensure_one()
        if not self.geotab_driver_id:
            raise UserError("Please select a Geotab driver profile to link.")

        gt_driver = self.geotab_driver_id
        contact = self.contact_id

        # Release old Geotab driver record if changing
        if contact.x_geotab_driver_id and contact.x_geotab_driver_id != gt_driver.geotab_id:
            old_rec = self.env["premafirm.geotab.driver"].search(
                [("geotab_id", "=", contact.x_geotab_driver_id)], limit=1
            )
            if old_rec:
                old_rec.write({"linked_contact_id": False})

        # Write link on contact
        write_vals = {
            "x_geotab_driver_id": gt_driver.geotab_id,
            "x_driver_sync_status": "pending",
            "x_driver_sync_error": False,
            "x_last_driver_sync_at": fields.Datetime.now(),
        }
        # Populate once-if-empty profile fields from Geotab
        if gt_driver.email and not contact.email:
            write_vals["email"] = gt_driver.email
        if gt_driver.employee_number and not contact.ref:
            write_vals["ref"] = gt_driver.employee_number

        contact.write(write_vals)

        # Mark Geotab driver as linked to this contact
        gt_driver.write({"linked_contact_id": contact.id})

        # Import today/week work summary from Geotab
        try:
            self._import_driver_summary(contact, gt_driver.geotab_id)
            msg = f"Contact linked to Geotab driver '{gt_driver.name}' and work data imported."
            contact.write({"x_driver_sync_status": "ok"})
        except Exception as exc:
            _logger.warning("Driver data import failed for %s: %s", contact.name, exc)
            contact.write({"x_driver_sync_status": "error", "x_driver_sync_error": str(exc)[:500]})
            msg = f"Contact linked to '{gt_driver.name}' but data import failed: {exc}"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Driver Linked",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }

    def _import_driver_summary(self, contact, geotab_driver_id):
        """Fetch last-7d duty status logs and trips, compute today/week summaries."""
        from ..services.geotab_service import GeotabService, _parse_geotab_dt
        from ..models.geotab_sync import _map_duty_status

        svc = GeotabService(self.env)
        now_utc = datetime.now(timezone.utc)
        week_ago = now_utc - timedelta(days=7)
        today = now_utc.date()
        week_start = today - timedelta(days=today.weekday())

        # --- Duty Status Logs → driving/on-duty hours ---
        # DutyStatusLog has no duration field; compute from consecutive timestamps.
        logs = svc.get_duty_status_logs(
            driver_id=geotab_driver_id,
            from_date=week_ago,
            to_date=now_utc,
        )

        parsed_logs = []
        for entry in logs:
            dt_str = entry.get("dateTime") or ""
            log_dt = _parse_geotab_dt(dt_str)
            if not log_dt:
                continue
            status = _map_duty_status(entry.get("status") or "")
            device_raw = entry.get("device")
            device_id = device_raw.get("id") if isinstance(device_raw, dict) else ""
            parsed_logs.append({"dt": log_dt, "status": status, "device_id": device_id or ""})

        parsed_logs.sort(key=lambda x: x["dt"])

        today_drive = today_on_duty = 0.0
        week_drive = week_on_duty = 0.0
        last_status = None
        last_shift_start = None
        last_vehicle_id = None

        for i, entry in enumerate(parsed_logs):
            log_dt = entry["dt"]
            log_date = log_dt.date()
            status = entry["status"]

            next_dt = parsed_logs[i + 1]["dt"] if i + 1 < len(parsed_logs) else now_utc
            duration_s = max(0.0, (next_dt - log_dt).total_seconds())

            drive_h = duration_s / 3600.0 if status == "D" else 0.0
            on_duty_h = duration_s / 3600.0 if status in ("D", "ON") else 0.0

            if log_date == today:
                today_drive += drive_h
                today_on_duty += on_duty_h
            if log_date >= week_start:
                week_drive += drive_h
                week_on_duty += on_duty_h

            last_status = status
            if last_shift_start is None or log_dt < last_shift_start:
                last_shift_start = log_dt

            if entry["device_id"]:
                veh = self.env["fleet.vehicle"].sudo().search(
                    [("x_geotab_device_id", "=", entry["device_id"])], limit=1
                )
                if veh:
                    last_vehicle_id = veh.id

        # --- Trips → distance (distance field is in metres) ---
        today_dist = week_dist = 0.0
        try:
            trips = svc.get_trips(
                driver_id=geotab_driver_id,
                from_date=week_ago,
                to_date=now_utc,
            )
            for trip in trips:
                trip_dt = _parse_geotab_dt(trip.get("start") or trip.get("dateTime") or "")
                trip_date = trip_dt.date() if trip_dt else None
                dist_km = float(trip.get("distance") or 0.0) / 1000.0
                if trip_date == today:
                    today_dist += dist_km
                if trip_date and trip_date >= week_start:
                    week_dist += dist_km
        except Exception as exc:
            _logger.warning("Trip distance fetch failed for driver %s: %s", geotab_driver_id, exc)

        update = {
            "x_today_driving_hours": round(today_drive, 2),
            "x_today_on_duty_hours": round(today_on_duty, 2),
            "x_today_distance_km": round(today_dist, 1),
            "x_week_driving_hours": round(week_drive, 2),
            "x_week_on_duty_hours": round(week_on_duty, 2),
            "x_week_distance_km": round(week_dist, 1),
            "x_last_log_sync_at": fields.Datetime.now(),
        }
        if last_status:
            update["x_last_duty_status"] = last_status
        if last_vehicle_id:
            update["x_last_known_vehicle_id"] = last_vehicle_id
        if last_shift_start:
            update["x_last_shift_start_at"] = last_shift_start.replace(tzinfo=None)

        contact.write(update)
