import logging
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PremafirmGeotabSync(models.Model):
    """Sync log + cron-callable sync methods for Geotab integration."""

    _name = "premafirm.geotab.sync"
    _description = "Geotab Sync Log"
    _order = "started_at desc"

    name = fields.Char(compute="_compute_name", store=True)
    sync_type = fields.Selection(
        [
            ("vehicles", "Vehicle Sync"),
            ("telematics", "Telematics Sync"),
            ("fuel_averages", "Fuel Average Sync"),
            ("drivers", "Driver Sync"),
            ("driver_logs", "Driver Log Sync"),
            ("daily_odometer", "Daily Odometer Sync"),
            ("all", "Full Sync"),
        ],
        required=True,
    )
    started_at = fields.Datetime(default=fields.Datetime.now)
    ended_at = fields.Datetime()
    status = fields.Selection(
        [("running", "Running"), ("done", "Done"), ("error", "Error")],
        default="running",
    )
    records_processed = fields.Integer(default=0)
    records_created = fields.Integer(default=0)
    records_updated = fields.Integer(default=0)
    error_message = fields.Text()

    @api.depends("sync_type", "started_at")
    def _compute_name(self):
        for rec in self:
            label = dict(self._fields["sync_type"].selection).get(rec.sync_type, "")
            dt = rec.started_at.strftime("%Y-%m-%d %H:%M") if rec.started_at else ""
            rec.name = f"{label} — {dt}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_service(self):
        from ..services.geotab_service import GeotabService
        return GeotabService(self.env)

    def _start_log(self, sync_type):
        return self.create({"sync_type": sync_type, "started_at": fields.Datetime.now(), "status": "running"})

    def _finish_log(self, log, processed=0, created=0, updated=0):
        log.write({
            "status": "done",
            "ended_at": fields.Datetime.now(),
            "records_processed": processed,
            "records_created": created,
            "records_updated": updated,
        })

    def _error_log(self, log, msg):
        log.write({"status": "error", "ended_at": fields.Datetime.now(), "error_message": str(msg)[:4000]})

    def _set_last_sync(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "geotab.last_sync_at", fields.Datetime.now().isoformat()
        )

    # ------------------------------------------------------------------
    # Vehicle Sync — hourly
    # ------------------------------------------------------------------

    @api.model
    def run_vehicle_sync(self):
        log = self._start_log("vehicles")
        try:
            svc = self._get_service()
            devices = svc.get_devices()
            Vehicle = self.env["fleet.vehicle"].sudo()
            created = updated = 0

            for device in devices:
                d_id = (device.get("id") or "").strip()
                d_vin = (device.get("vehicleIdentificationNumber") or "").strip()
                d_plate = (device.get("licensePlate") or "").strip()
                d_name = (device.get("name") or "").strip()
                if not d_id:
                    continue

                # Try to find existing vehicle
                vehicle = Vehicle.search([("x_geotab_device_id", "=", d_id)], limit=1)
                if not vehicle and d_vin:
                    vehicle = Vehicle.search([("vin_sn", "=", d_vin)], limit=1)
                if not vehicle and d_plate:
                    vehicle = Vehicle.search([("license_plate", "=", d_plate)], limit=1)
                if not vehicle and d_name:
                    vehicle = Vehicle.search([("name", "ilike", d_name)], limit=1)

                geotab_vals = {
                    "x_geotab_device_id": d_id,
                    "x_external_source": "geotab",
                    "x_last_eld_sync_at": fields.Datetime.now(),
                    "x_sync_status": "ok",
                }
                # Enrich once-if-empty fields
                if d_vin:
                    geotab_vals.setdefault("vin_sn", d_vin)
                if d_plate:
                    geotab_vals.setdefault("license_plate", d_plate)
                if d_name and not vehicle:
                    geotab_vals.setdefault("name", d_name)

                if vehicle:
                    # Only update Geotab-owned fields
                    write_vals = {"x_geotab_device_id": d_id, "x_last_eld_sync_at": fields.Datetime.now(), "x_sync_status": "ok"}
                    if not vehicle.vin_sn and d_vin:
                        write_vals["vin_sn"] = d_vin
                    if not vehicle.license_plate and d_plate:
                        write_vals["license_plate"] = d_plate
                    vehicle.write(write_vals)
                    updated += 1
                else:
                    # Create new vehicle record if name is available
                    if not d_name:
                        _logger.warning("Geotab device %s has no name, skipping create.", d_id)
                        continue
                    # Provide required fields for fleet.vehicle
                    brand_id = self.env["fleet.vehicle.model.brand"].sudo().search([], limit=1)
                    model_id = self.env["fleet.vehicle.model"].sudo().search([], limit=1)
                    if not model_id:
                        _logger.warning("No fleet.vehicle.model found, cannot auto-create truck for device %s", d_id)
                        continue
                    Vehicle.create({
                        "name": d_name,
                        "model_id": model_id.id,
                        "vin_sn": d_vin or False,
                        "license_plate": d_plate or False,
                        "x_geotab_device_id": d_id,
                        "x_external_source": "geotab",
                        "x_last_eld_sync_at": fields.Datetime.now(),
                        "x_sync_status": "ok",
                    })
                    created += 1

            self._set_last_sync()
            self._finish_log(log, len(devices), created, updated)
            _logger.info("Geotab vehicle sync done: %d devices, %d created, %d updated", len(devices), created, updated)
        except Exception as exc:
            _logger.exception("Geotab vehicle sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Telematics Sync — every 15-30 min
    # ------------------------------------------------------------------

    @api.model
    def run_telematics_sync(self):
        log = self._start_log("telematics")
        try:
            svc = self._get_service()
            vehicles = self.env["fleet.vehicle"].sudo().search([("x_geotab_device_id", "!=", False)])
            updated = 0

            for vehicle in vehicles:
                d_id = vehicle.x_geotab_device_id
                try:
                    # DeviceStatusInfo for location
                    status_list = svc.get_device_status_info(d_id)
                    status_rec = status_list[0] if status_list else None
                    lat, lng, loc_dt = svc.extract_location(status_rec)

                    # StatusData for odometer
                    odo_rec = svc.get_status_data(d_id, "DiagnosticOdometerId")
                    odometer_km = svc.extract_odometer_km(odo_rec)

                    # StatusData for engine hours
                    eng_rec = svc.get_status_data(d_id, "DiagnosticEngineHoursId")
                    engine_hours = svc.extract_engine_hours(eng_rec)

                    # Fuel % — tries percentage diagnostic first, falls back to direct-% form
                    from ..services.geotab_service import fetch_fuel_percent, fetch_def_percent
                    fuel_pct = fetch_fuel_percent(svc, d_id)

                    # DEF % — tries standard diagnostic first, falls back to OBD direct form
                    def_pct = fetch_def_percent(svc, d_id)

                    write_vals = {
                        "x_last_eld_sync_at": fields.Datetime.now(),
                        "x_sync_status": "ok",
                        "x_sync_error": False,
                    }
                    if odometer_km is not None:
                        write_vals["x_current_odometer_km"] = odometer_km
                    if engine_hours is not None:
                        write_vals["x_current_engine_hours"] = engine_hours
                    # Only overwrite fuel/DEF when the new reading is > 0.
                    # A 0 from GeoTab means the engine is off / no data — not an empty tank.
                    got_valid_reading = False
                    if fuel_pct is not None and fuel_pct > 0:
                        write_vals["x_current_fuel_percent"] = fuel_pct
                        got_valid_reading = True
                    if def_pct is not None and def_pct > 0:
                        write_vals["x_current_def_percent"] = def_pct
                        got_valid_reading = True
                    if got_valid_reading:
                        write_vals["x_last_valid_telematics_at"] = fields.Datetime.now()
                    if lat is not None:
                        write_vals["x_last_location_lat"] = lat
                    if lng is not None:
                        write_vals["x_last_location_lng"] = lng
                    if loc_dt is not None:
                        write_vals["x_last_location_at"] = loc_dt.replace(tzinfo=None)

                    # Reverse-geocode to a human-readable address when coordinates changed
                    if lat is not None and lng is not None:
                        try:
                            from ..services.mapbox_service import MapboxService
                            addr = MapboxService(self.env).reverse_geocode(lat, lng)
                            if addr:
                                write_vals["x_last_location_address"] = addr
                        except Exception as geo_exc:
                            _logger.debug("Reverse geocode failed for %s: %s", vehicle.name, geo_exc)

                    vehicle.write(write_vals)
                    updated += 1
                except Exception as exc:
                    _logger.warning("Telematics sync failed for vehicle %s: %s", vehicle.name, exc)
                    vehicle.write({"x_sync_status": "error", "x_sync_error": str(exc)[:500]})

            self._finish_log(log, len(vehicles), 0, updated)
            _logger.info("Geotab telematics sync done: %d vehicles updated", updated)
        except Exception as exc:
            _logger.exception("Geotab telematics sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Weekly Fuel Average Sync — every Sunday midnight
    # ------------------------------------------------------------------

    @api.model
    def run_fuel_averages_sync(self):
        log = self._start_log("fuel_averages")
        try:
            svc = self._get_service()
            now_utc = datetime.now(timezone.utc)
            from_date, to_date, period_label = _fuel_period_dates(now_utc)
            vehicles = self.env["fleet.vehicle"].sudo().search([("x_geotab_device_id", "!=", False)])
            updated = 0

            for vehicle in vehicles:
                d_id = vehicle.x_geotab_device_id
                try:
                    avg_km_l, avg_l_100km = _compute_fuel_avg(
                        svc, d_id, from_date, to_date, vehicle.name,
                        tank_capacity_l=vehicle.x_tank_capacity_l or 0.0,
                    )
                    if avg_km_l is None:
                        continue
                    vehicle.write({
                        "x_avg_km_per_l_last_week": avg_km_l,
                        "x_avg_l_per_100km_last_week": avg_l_100km,
                        "x_last_fuel_sync_at": fields.Datetime.now(),
                    })
                    updated += 1
                except Exception as exc:
                    _logger.warning("Fuel avg sync failed for vehicle %s: %s", vehicle.name, exc)

            self._finish_log(log, len(vehicles), 0, updated)
            _logger.info("Geotab fuel average sync done (%s): %d vehicles updated", period_label, updated)
        except Exception as exc:
            _logger.exception("Geotab fuel average sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Driver Sync — hourly
    # ------------------------------------------------------------------

    @api.model
    def run_driver_sync(self):
        log = self._start_log("drivers")
        try:
            svc = self._get_service()
            gt_drivers = svc.get_drivers()
            Partner = self.env["res.partner"].sudo()
            auto_create = self.env["ir.config_parameter"].sudo().get_param(
                "geotab.auto_create_drivers", "False"
            ).lower() == "true"

            # Only contacts tagged "Driver"
            driver_contacts = Partner.search([("x_is_driver_profile", "=", True)])
            created = updated = 0

            for gt_driver in gt_drivers:
                d_id = (gt_driver.get("id") or "").strip()
                d_email = (gt_driver.get("email") or "").strip().lower()
                d_first = (gt_driver.get("firstName") or "").strip()
                d_last = (gt_driver.get("lastName") or "").strip()
                d_full = f"{d_first} {d_last}".strip()
                if not d_id:
                    continue

                # Match priority: already linked → email → full name
                match = driver_contacts.filtered(lambda c: c.x_geotab_driver_id == d_id)
                if not match and d_email:
                    match = driver_contacts.filtered(lambda c: (c.email or "").strip().lower() == d_email)
                if not match and d_full:
                    match = driver_contacts.filtered(lambda c: (c.name or "").strip().lower() == d_full.lower())

                driver_vals = {
                    "x_geotab_driver_id": d_id,
                    "x_last_driver_sync_at": fields.Datetime.now(),
                    "x_driver_sync_status": "ok",
                    "x_driver_sync_error": False,
                }

                if match:
                    match[0].write(driver_vals)
                    updated += 1
                elif auto_create and d_full:
                    # Create new contact with Driver tag
                    driver_tag = self.env["res.partner.category"].sudo().search(
                        [("name", "ilike", "Driver")], limit=1
                    )
                    if not driver_tag:
                        driver_tag = self.env["res.partner.category"].sudo().create({"name": "Driver"})
                    driver_vals.update({
                        "name": d_full,
                        "email": d_email or False,
                        "category_id": [(4, driver_tag.id)],
                        "x_driver_status": "active",
                        "x_driver_timezone": "America/Toronto",
                    })
                    Partner.create(driver_vals)
                    created += 1
                else:
                    _logger.info("Geotab driver %s (%s) — no match, skipping (auto_create disabled).", d_id, d_full)

            self._finish_log(log, len(gt_drivers), created, updated)
            _logger.info("Geotab driver sync done: %d drivers, %d created, %d updated", len(gt_drivers), created, updated)
        except Exception as exc:
            _logger.exception("Geotab driver sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Driver Log Sync — hourly
    # ------------------------------------------------------------------

    @api.model
    def run_driver_log_sync(self):
        log = self._start_log("driver_logs")
        try:
            svc = self._get_service()
            now_utc = datetime.now(timezone.utc)
            day_ago = now_utc - timedelta(hours=25)  # slight overlap to catch boundary logs
            week_ago = now_utc - timedelta(days=7)

            linked_drivers = self.env["res.partner"].sudo().search([
                ("x_geotab_driver_id", "!=", False),
                ("x_is_driver_profile", "=", True),
            ])
            DriverLog = self.env["fleet.driver.log"].sudo()
            processed = created = 0

            for driver in linked_drivers:
                d_id = driver.x_geotab_driver_id
                try:
                    logs = svc.get_duty_status_logs(d_id, from_date=day_ago, to_date=now_utc)
                    today = now_utc.date()
                    week_start = today - timedelta(days=today.weekday())

                    today_driving = today_on_duty = today_distance = 0.0
                    week_driving = week_on_duty = week_distance = 0.0
                    last_status = None
                    last_shift_start = None
                    last_vehicle = None

                    for entry in logs:
                        ref_id = (entry.get("id") or "").strip()
                        status_code = _map_duty_status(entry.get("status") or "")
                        dt_str = entry.get("dateTime") or entry.get("startDateTime") or ""
                        from ..services.geotab_service import _parse_geotab_dt
                        log_dt = _parse_geotab_dt(dt_str)
                        log_date_val = log_dt.date() if log_dt else today

                        dist_m = float(entry.get("distanceDriven") or 0.0)
                        dist_km = dist_m / 1000.0
                        duration_s = float(entry.get("duration") or 0.0)
                        driving_h = duration_s / 3600.0 if status_code == "D" else 0.0
                        on_duty_h = duration_s / 3600.0 if status_code in ("D", "ON") else 0.0

                        # Accumulate totals
                        if log_date_val == today:
                            today_driving += driving_h
                            today_on_duty += on_duty_h
                            today_distance += dist_km
                        if log_date_val >= week_start:
                            week_driving += driving_h
                            week_on_duty += on_duty_h
                            week_distance += dist_km

                        if log_dt:
                            if last_shift_start is None or log_dt < last_shift_start:
                                last_shift_start = log_dt
                        last_status = status_code

                        # Device (vehicle) linked in log
                        device = entry.get("device") or {}
                        device_id = device.get("id") or ""
                        if device_id:
                            vehicle = self.env["fleet.vehicle"].sudo().search(
                                [("x_geotab_device_id", "=", device_id)], limit=1
                            )
                            if vehicle:
                                last_vehicle = vehicle

                        # Upsert driver log record
                        existing = DriverLog.search([
                            ("driver_contact_id", "=", driver.id),
                            ("raw_external_ref", "=", ref_id),
                        ], limit=1) if ref_id else None

                        log_vals = {
                            "driver_contact_id": driver.id,
                            "vehicle_id": last_vehicle.id if last_vehicle else False,
                            "source": "geotab",
                            "log_date": log_date_val,
                            "start_datetime": log_dt.replace(tzinfo=None) if log_dt else False,
                            "duty_status": status_code,
                            "driving_hours": round(driving_h, 2),
                            "on_duty_hours": round(on_duty_h, 2),
                            "distance_km": round(dist_km, 1),
                            "raw_external_ref": ref_id or False,
                        }
                        if existing:
                            existing.write(log_vals)
                        else:
                            DriverLog.create(log_vals)
                            created += 1
                        processed += 1

                    # Update summary fields on driver contact
                    driver_update = {
                        "x_today_driving_hours": round(today_driving, 2),
                        "x_today_on_duty_hours": round(today_on_duty, 2),
                        "x_today_distance_km": round(today_distance, 1),
                        "x_week_driving_hours": round(week_driving, 2),
                        "x_week_on_duty_hours": round(week_on_duty, 2),
                        "x_week_distance_km": round(week_distance, 1),
                        "x_last_log_sync_at": fields.Datetime.now(),
                    }
                    if last_status:
                        driver_update["x_last_duty_status"] = last_status
                    if last_vehicle:
                        driver_update["x_last_known_vehicle_id"] = last_vehicle.id
                    if last_shift_start:
                        driver_update["x_last_shift_start_at"] = last_shift_start.replace(tzinfo=None)
                    driver.write(driver_update)

                except Exception as exc:
                    _logger.warning("Driver log sync failed for driver %s: %s", driver.name, exc)

            self._finish_log(log, processed, created, 0)
            _logger.info("Geotab driver log sync done: %d entries, %d created", processed, created)
        except Exception as exc:
            _logger.exception("Geotab driver log sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Single-vehicle full import (called after manual Geotab link)
    # ------------------------------------------------------------------

    @api.model
    def import_vehicle_data(self, vehicle):
        """Import all available Geotab data for a single vehicle that was just linked."""
        from ..services.geotab_service import GeotabService, _parse_geotab_dt

        d_id = vehicle.x_geotab_device_id
        if not d_id:
            raise ValueError("Vehicle has no Geotab device ID linked.")

        svc = GeotabService(self.env)
        write_vals = {
            "x_last_eld_sync_at": fields.Datetime.now(),
            "x_sync_status": "ok",
            "x_sync_error": False,
        }

        # --- Live location ---
        try:
            status_list = svc.get_device_status_info(d_id)
            status_rec = status_list[0] if status_list else None
            lat, lng, loc_dt = svc.extract_location(status_rec)
            if lat is not None:
                write_vals["x_last_location_lat"] = lat
            if lng is not None:
                write_vals["x_last_location_lng"] = lng
            if loc_dt is not None:
                write_vals["x_last_location_at"] = loc_dt.replace(tzinfo=None)
            if lat is not None and lng is not None:
                try:
                    from ..services.mapbox_service import MapboxService
                    addr = MapboxService(self.env).reverse_geocode(lat, lng)
                    if addr:
                        write_vals["x_last_location_address"] = addr
                except Exception as geo_exc:
                    _logger.debug("Reverse geocode failed for %s: %s", vehicle.name, geo_exc)
        except Exception as exc:
            _logger.warning("Could not fetch location for %s: %s", vehicle.name, exc)

        # --- Odometer ---
        try:
            odo_rec = svc.get_status_data(d_id, "DiagnosticOdometerId")
            km = svc.extract_odometer_km(odo_rec)
            if km is not None:
                write_vals["x_current_odometer_km"] = km
        except Exception as exc:
            _logger.warning("Could not fetch odometer for %s: %s", vehicle.name, exc)

        # --- Engine hours ---
        try:
            eng_rec = svc.get_status_data(d_id, "DiagnosticEngineHoursId")
            hours = svc.extract_engine_hours(eng_rec)
            if hours is not None:
                write_vals["x_current_engine_hours"] = hours
        except Exception as exc:
            _logger.warning("Could not fetch engine hours for %s: %s", vehicle.name, exc)

        # --- Fuel % --- tries percentage form first, falls back to direct-% OBD form
        try:
            from ..services.geotab_service import fetch_fuel_percent, fetch_def_percent
            pct = fetch_fuel_percent(svc, d_id)
            if pct is not None and pct > 0:
                write_vals["x_current_fuel_percent"] = pct
                write_vals["x_last_valid_telematics_at"] = fields.Datetime.now()
        except Exception as exc:
            _logger.warning("Could not fetch fuel level for %s: %s", vehicle.name, exc)

        # --- DEF Level % --- tries standard form first, falls back to OBD direct form
        try:
            from ..services.geotab_service import fetch_def_percent
            def_pct = fetch_def_percent(svc, d_id)
            if def_pct is not None and def_pct > 0:
                write_vals["x_current_def_percent"] = def_pct
                write_vals.setdefault("x_last_valid_telematics_at", fields.Datetime.now())
        except Exception as exc:
            _logger.warning("Could not fetch DEF level for %s: %s", vehicle.name, exc)

        # --- Monthly fuel average ---
        try:
            now_utc = datetime.now(timezone.utc)
            from_date, to_date, period_label = _fuel_period_dates(now_utc)
            avg_km_l, avg_l_100km = _compute_fuel_avg(
                svc, d_id, from_date, to_date, vehicle.name,
                tank_capacity_l=vehicle.x_tank_capacity_l or 0.0,
            )
            if avg_km_l is not None:
                write_vals["x_avg_km_per_l_last_week"] = avg_km_l
                write_vals["x_avg_l_per_100km_last_week"] = avg_l_100km
                write_vals["x_last_fuel_sync_at"] = fields.Datetime.now()
                _logger.info("Fuel avg for %s (%s): %.2f km/L  %.2f L/100km", vehicle.name, period_label, avg_km_l, avg_l_100km)
        except Exception as exc:
            _logger.warning("Could not fetch fuel averages for %s: %s", vehicle.name, exc)

        vehicle.write(write_vals)
        _logger.info("Imported Geotab data for vehicle %s (device %s)", vehicle.name, d_id)

    # ------------------------------------------------------------------
    # Daily Odometer Sync — midnight cron
    # ------------------------------------------------------------------

    @api.model
    def run_daily_odometer_sync(self):
        """Snapshot daily odometer + engine hours + GeoTab driver for each linked vehicle."""
        log = self._start_log("daily_odometer")
        try:
            from datetime import date, datetime, timedelta, timezone
            svc = self._get_service()
            today = date.today()
            yesterday = today - timedelta(days=1)
            day_start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=timezone.utc)
            day_end = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=timezone.utc)

            vehicles = self.env["fleet.vehicle"].sudo().search([("x_geotab_device_id", "!=", False)])
            DailyOdo = self.env["fleet.daily.odometer"].sudo()
            created = updated = 0

            for vehicle in vehicles:
                d_id = vehicle.x_geotab_device_id
                try:
                    # StatusData is reliable even on days with no trips
                    odo_rec = svc.get_status_data(d_id, "DiagnosticOdometerId", from_date=day_start, to_date=day_end)
                    eng_rec = svc.get_status_data(d_id, "DiagnosticEngineHoursId", from_date=day_start, to_date=day_end)
                    odometer_km = round(float(odo_rec.get("data", 0)) / 1000.0, 1) if odo_rec else vehicle.x_current_odometer_km
                    engine_hours = round(float(eng_rec.get("data", 0)) / 3600.0, 1) if eng_rec else vehicle.x_current_engine_hours

                    if not odometer_km:
                        _logger.info("Vehicle %s: no odometer data, skipping daily log for %s.", vehicle.name, yesterday)
                        continue

                    # Driver from trips (may be empty on rest days)
                    trips = svc.get_trips(d_id, from_date=day_start, to_date=day_end)
                    driver_distance = {}
                    for t in trips:
                        drv = t.get("driver")
                        if isinstance(drv, dict):
                            drv_id = (drv.get("id") or "").strip()
                            if drv_id:
                                driver_distance[drv_id] = driver_distance.get(drv_id, 0.0) + float(t.get("distance") or 0.0)

                    gt_driver_id = max(driver_distance, key=driver_distance.get) if driver_distance else None
                    driver_contact = False
                    if gt_driver_id:
                        driver_contact = self.env["res.partner"].sudo().search(
                            [("x_geotab_driver_id", "=", gt_driver_id), ("x_is_driver_profile", "=", True)],
                            limit=1,
                        )

                    vals = {
                        "vehicle_id": vehicle.id,
                        "log_date": yesterday,
                        "odometer_km": round(odometer_km, 1),
                        "engine_hours": round(engine_hours, 1) if engine_hours else 0.0,
                        "driver_contact_id": driver_contact.id if driver_contact else False,
                        "source": "geotab",
                    }
                    existing = DailyOdo.search([
                        ("vehicle_id", "=", vehicle.id),
                        ("log_date", "=", yesterday),
                    ], limit=1)
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        DailyOdo.create(vals)
                        created += 1

                except Exception as exc:
                    _logger.warning("Daily odometer sync failed for vehicle %s: %s", vehicle.name, exc)

            self._finish_log(log, len(vehicles), created, updated)
            _logger.info("Daily odometer sync done: %d created, %d updated", created, updated)
        except Exception as exc:
            _logger.exception("Daily odometer sync failed")
            self._error_log(log, exc)

    # ------------------------------------------------------------------
    # Per-vehicle odometer import (manual button + initial backfill)
    # ------------------------------------------------------------------

    @api.model
    def import_daily_odometer(self, vehicle):
        """Import last 7 days of odometer/engine hours/driver for a single vehicle."""
        from datetime import date, datetime, timedelta, timezone
        from ..services.geotab_service import GeotabService

        d_id = vehicle.x_geotab_device_id
        if not d_id:
            raise ValueError("Vehicle has no Geotab device ID linked.")

        svc = GeotabService(self.env)
        DailyOdo = self.env["fleet.daily.odometer"].sudo()
        today = date.today()

        for days_back in range(1, 8):  # yesterday back to 7 days ago
            log_date = today - timedelta(days=days_back)
            day_start = datetime(log_date.year, log_date.month, log_date.day, 0, 0, 0, tzinfo=timezone.utc)
            day_end = datetime(log_date.year, log_date.month, log_date.day, 23, 59, 59, tzinfo=timezone.utc)

            try:
                # Odometer and engine hours always come from StatusData (reliable even on no-trip days)
                odo_rec = svc.get_status_data(d_id, "DiagnosticOdometerId", from_date=day_start, to_date=day_end)
                eng_rec = svc.get_status_data(d_id, "DiagnosticEngineHoursId", from_date=day_start, to_date=day_end)

                odometer_km = round(float(odo_rec.get("data", 0)) / 1000.0, 1) if odo_rec else 0.0
                engine_hours = round(float(eng_rec.get("data", 0)) / 3600.0, 1) if eng_rec else 0.0

                if not odometer_km:
                    _logger.info("No odometer data for vehicle %s on %s, skipping.", vehicle.name, log_date)
                    continue

                # Driver: use trips to find who drove the most distance that day
                trips = svc.get_trips(d_id, from_date=day_start, to_date=day_end)
                driver_distance = {}
                for t in trips:
                    drv = t.get("driver")
                    if isinstance(drv, dict):
                        drv_id = (drv.get("id") or "").strip()
                        if drv_id:
                            driver_distance[drv_id] = driver_distance.get(drv_id, 0.0) + float(t.get("distance") or 0.0)

                gt_driver_id = max(driver_distance, key=driver_distance.get) if driver_distance else None
                driver_contact = False
                if gt_driver_id:
                    driver_contact = self.env["res.partner"].sudo().search(
                        [("x_geotab_driver_id", "=", gt_driver_id), ("x_is_driver_profile", "=", True)],
                        limit=1,
                    )

                vals = {
                    "vehicle_id": vehicle.id,
                    "log_date": log_date,
                    "odometer_km": odometer_km,
                    "engine_hours": engine_hours,
                    "driver_contact_id": driver_contact.id if driver_contact else False,
                    "source": "geotab",
                }
                existing = DailyOdo.search([
                    ("vehicle_id", "=", vehicle.id),
                    ("log_date", "=", log_date),
                ], limit=1)
                if existing:
                    existing.write(vals)
                else:
                    DailyOdo.create(vals)
                _logger.info("Daily odo logged for %s on %s: %.1f km, %.1f h, driver=%s",
                             vehicle.name, log_date, odometer_km, engine_hours,
                             driver_contact.name if driver_contact else "none")

            except Exception as exc:
                _logger.warning("Daily odometer import failed for %s on %s: %s", vehicle.name, log_date, exc)

        _logger.info("Daily odometer import complete for vehicle %s", vehicle.name)

    # ------------------------------------------------------------------
    # Run all syncs
    # ------------------------------------------------------------------

    @api.model
    def run_all_syncs(self):
        self.run_vehicle_sync()
        self.run_telematics_sync()
        self.run_fuel_averages_sync()
        self.run_driver_sync()
        self.run_driver_log_sync()

    @api.model
    def run_monthly_avg_km_update(self):
        """2nd-of-month cron: update x_monthly_avg_km for all GeoTab-linked vehicles
        using actual previous calendar month km from fleet.daily.odometer."""
        from datetime import date, timedelta
        today = date.today()
        last_of_prev = today.replace(day=1) - timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)

        vehicles = self.env["fleet.vehicle"].sudo().search([
            ("active", "=", True),
            ("x_geotab_device_id", "!=", False),
        ])
        updated = 0
        for vehicle in vehicles:
            self.env.cr.execute(
                """
                SELECT MAX(odometer_km) - MIN(odometer_km)
                FROM   fleet_daily_odometer
                WHERE  vehicle_id = %s
                  AND  log_date  >= %s
                  AND  log_date  <= %s
                """,
                (vehicle.id, first_of_prev, last_of_prev),
            )
            row = self.env.cr.fetchone()
            km = float(row[0]) if row and row[0] is not None else 0.0
            if km > 0:
                vehicle.write({"x_monthly_avg_km": km})
                updated += 1
                _logger.info(
                    "Monthly km update: %s → %.1f km (%s)",
                    vehicle.name, km, last_of_prev.strftime("%B %Y"),
                )
        _logger.info("run_monthly_avg_km_update complete: %d/%d vehicles updated.", updated, len(vehicles))


def _fuel_period_dates(now_utc):
    """Return (from_date, to_date, label) for the fuel averaging window.

    Day 1–7:  full previous month (enough data guaranteed).
    Day 8+:   current month from the 1st through now (month-to-date).
    """
    import calendar
    if now_utc.day >= 8:
        from_date = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        to_date = now_utc
        label = now_utc.strftime("%B %Y MTD")
    else:
        if now_utc.month == 1:
            y, m = now_utc.year - 1, 12
        else:
            y, m = now_utc.year, now_utc.month - 1
        last_day = calendar.monthrange(y, m)[1]
        from_date = datetime(y, m, 1, 0, 0, 0, tzinfo=timezone.utc)
        to_date = datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc)
        label = from_date.strftime("%B %Y")
    return from_date, to_date, label


def _compute_fuel_avg(svc, device_id, from_date, to_date, vehicle_name="", tank_capacity_l=0.0):
    """Return (avg_km_per_l, avg_l_per_100km) for the given period, or (None, None).

    Sources tried in order (first non-zero wins):
      1. Trip.fuelConsumed               — CAN-bus direct fuel volume (liters)
      2. Trip.averageFuelEconomy         — Geotab-computed L/100km per trip
      3. DiagnosticEngineTotalFuelUsedId — J1939 cumulative counter delta (liters)
      4. FuelTransaction.volume          — fill-up records (liters)
      5. Fuel level change integration   — sum of fuel-level drops × tank capacity
    """
    from ..services.geotab_service import DIAG_TOTAL_FUEL_USED, DIAG_FUEL_PERCENT, DIAG_FUEL_LEVEL

    trips = svc.get_trips(device_id, from_date=from_date, to_date=to_date)

    # Geotab Trip.distance comes back in km on this server (not meters).
    # Log a sample so we can verify the unit in future.
    if trips:
        sample = trips[0]
        _logger.info(
            "Fuel avg [%s]: sample trip keys=%s distance=%s fuelConsumed=%s averageFuelEconomy=%s",
            vehicle_name, list(sample.keys()),
            sample.get("distance"), sample.get("fuelConsumed"), sample.get("averageFuelEconomy"),
        )

    total_km = sum(float(t.get("distance") or 0.0) for t in trips)   # km directly
    total_fuel_l = sum(float(t.get("fuelConsumed") or 0.0) for t in trips)

    _logger.info(
        "Fuel avg [%s] %s→%s: %d trips  %.1f km  %.3f L (Trip.fuelConsumed)",
        vehicle_name, from_date.date(), to_date.date(), len(trips), total_km, total_fuel_l,
    )

    # Source 2: averageFuelEconomy per trip (L/100km) — distance already in km
    if total_km > 0 and not total_fuel_l:
        derived = 0.0
        for t in trips:
            eco = float(t.get("averageFuelEconomy") or 0.0)
            dist_km = float(t.get("distance") or 0.0)   # km directly
            if eco > 0.0 and dist_km > 0.0:
                derived += eco * dist_km / 100.0
        _logger.info("Fuel avg [%s]: source2 averageFuelEconomy → %.3f L derived", vehicle_name, derived)
        if derived:
            total_fuel_l = derived

    # Source 3: J1939 cumulative total-fuel-used counter delta
    if total_km > 0 and not total_fuel_l:
        try:
            # Latest reading at or before start of period
            rec_s = svc.get_status_data(device_id, DIAG_TOTAL_FUEL_USED, to_date=from_date)
            # Latest reading at or before end of period
            rec_e = svc.get_status_data(device_id, DIAG_TOTAL_FUEL_USED, to_date=to_date)
            sv = float((rec_s or {}).get("data") or 0.0)
            ev = float((rec_e or {}).get("data") or 0.0)
            _logger.info(
                "Fuel avg [%s]: source3 TotalFuelUsed start=%.1f end=%.1f delta=%.1f L",
                vehicle_name, sv, ev, ev - sv if ev > sv else 0,
            )
            if ev > sv > 0:
                total_fuel_l = ev - sv
        except Exception as exc:
            _logger.warning("Fuel avg [%s]: source3 TotalFuelUsed failed: %s", vehicle_name, exc)

    # Source 4: FuelTransaction fill-up records
    if total_km > 0 and not total_fuel_l:
        try:
            txns = svc.get_fuel_transactions(device_id, from_date=from_date, to_date=to_date)
            txn_total = sum(float(t.get("volume") or 0.0) for t in txns)
            _logger.info(
                "Fuel avg [%s]: source4 FuelTransaction %d records volumes=%s → %.1f L",
                vehicle_name, len(txns),
                [round(float(t.get("volume") or 0), 1) for t in txns[:8]],
                txn_total,
            )
            # Diagnostic: if still 0, log all FuelTransactions in the account (no filter)
            if not txns:
                try:
                    all_ft = svc.call("Get", type_name="FuelTransaction", search={}) or []
                    device_ids = list({(t.get("device") or {}).get("id", "?") for t in all_ft})
                    ft_sample = [{k: t.get(k) for k in ["device", "dateTime", "volume", "vehicleIdentificationNumber"]} for t in all_ft[:3]]
                    _logger.info(
                        "Fuel avg [%s]: FuelTransaction ALL (no filter) = %d records, device IDs in DB: %s, sample: %s",
                        vehicle_name, len(all_ft), device_ids[:10], ft_sample,
                    )
                except Exception as diag_exc:
                    _logger.info("Fuel avg [%s]: FuelTransaction all-query failed: %s", vehicle_name, diag_exc)
            if txn_total:
                total_fuel_l = txn_total
        except Exception as exc:
            _logger.warning("Fuel avg [%s]: source4 FuelTransaction failed: %s", vehicle_name, exc)

    # Source 5: Fuel level sensor change integration
    # Sum all downward movements in fuel level % × tank capacity = liters consumed.
    # This is exactly how MyGeotab's fuelEvents page computes consumption when CAN-bus
    # fuel counters are not available.
    if total_km > 0 and not total_fuel_l:
        if tank_capacity_l and tank_capacity_l > 0:
            try:
                # Try 0.0–1.0 scale first, fall back to 0–100 direct
                level_recs = svc.get_status_data_all(device_id, DIAG_FUEL_PERCENT, from_date, to_date)
                pct_scale = 100.0  # raw 0–1 → multiply by 100
                if not level_recs:
                    level_recs = svc.get_status_data_all(device_id, DIAG_FUEL_LEVEL, from_date, to_date)
                    pct_scale = 1.0  # already 0–100

                _logger.info("Fuel avg [%s]: source5 fuel-level readings: %d records (scale×%.0f)",
                             vehicle_name, len(level_recs), pct_scale)

                if level_recs:
                    # Filter out off/bad readings (below 2%), then sort by time
                    valid = [r for r in level_recs
                             if float(r.get("data") or 0.0) * pct_scale > 2.0]
                    valid.sort(key=lambda x: x.get("dateTime", ""))
                    _logger.info("Fuel avg [%s]: source5 valid readings after filter: %d", vehicle_name, len(valid))

                    if valid:
                        # Segment-based approach (matches Geotab's fuelEvents algorithm):
                        # Detect fill-ups as upward jumps ≥ 10%; for each segment
                        # between fill-ups take the NET drop — no noise amplification.
                        FILLUP_THRESHOLD_PCT = 10.0

                        seg_start = float(valid[0].get("data") or 0.0) * pct_scale
                        prev_pct = seg_start
                        segments = []  # (start%, end%) per consumption segment

                        for rec in valid[1:]:
                            pct = float(rec.get("data") or 0.0) * pct_scale
                            if pct - prev_pct >= FILLUP_THRESHOLD_PCT:
                                segments.append((seg_start, prev_pct))
                                seg_start = pct
                            prev_pct = pct
                        segments.append((seg_start, prev_pct))  # final segment

                        total_drop_pct = sum(max(0.0, s - e) for s, e in segments)
                        fuel_from_level = total_drop_pct / 100.0 * tank_capacity_l

                        _logger.info(
                            "Fuel avg [%s]: source5 %d segments, %.1f%% consumed × %.1fL tank = %.1f L",
                            vehicle_name, len(segments), total_drop_pct, tank_capacity_l, fuel_from_level,
                        )
                        if fuel_from_level > 0:
                            total_fuel_l = fuel_from_level
            except Exception as exc:
                _logger.warning("Fuel avg [%s]: source5 FuelLevelChange failed: %s", vehicle_name, exc)
        else:
            _logger.info(
                "Fuel avg [%s]: source5 skipped — Tank Capacity (L) not set on vehicle.",
                vehicle_name,
            )

    if total_km < 10.0:
        _logger.info("Fuel avg [%s]: only %.1f km in period — skipping.", vehicle_name, total_km)
        return None, None
    if not total_fuel_l:
        _logger.warning(
            "Fuel avg [%s]: all 5 sources returned 0 fuel (trips=%d km=%.1f). "
            "Set Tank Capacity (L) on the vehicle so fuel-level integration can be used.",
            vehicle_name, len(trips), total_km,
        )
        return None, None

    avg_km_l = round(total_km / total_fuel_l, 2)
    avg_l_100 = round(total_fuel_l / total_km * 100.0, 2)
    _logger.info("Fuel avg [%s]: RESULT %.2f km/L  %.2f L/100km  (%.1f km / %.1f L)",
                 vehicle_name, avg_km_l, avg_l_100, total_km, total_fuel_l)
    return avg_km_l, avg_l_100


def _map_duty_status(raw):
    mapping = {
        "D": "D", "Driving": "D",
        "ON": "ON", "OnDuty": "ON", "On_Duty": "ON",
        "OFF": "OFF", "OffDuty": "OFF", "Off_Duty": "OFF",
        "SB": "SB", "SleeperBerth": "SB", "Sleeper_Berth": "SB",
    }
    return mapping.get(raw, "OFF")
