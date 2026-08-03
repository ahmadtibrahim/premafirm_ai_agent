from odoo import api, fields, models


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    # ------------------------------------------------------------------
    # Legacy fields (preserved)
    # ------------------------------------------------------------------

    service_type = fields.Selection(
        [("dry", "Dry"), ("reefer", "Reefer")],
        default="dry",
        string="Service Type",
    )
    load_type = fields.Selection(
        [("LTL", "LTL"), ("FTL", "FTL")],
        default="FTL",
        string="Load Type",
    )
    home_location = fields.Char(string="Home Location")
    payload_capacity_lbs = fields.Float(default=40000.0)
    driver_id = fields.Many2one("res.partner", domain=[("is_driver", "=", True)])
    home_latitude = fields.Float()
    home_longitude = fields.Float()
    work_start_hour = fields.Float(string="Work Start Hour", default=8.0)
    vehicle_work_start_time = fields.Float(related="work_start_hour", readonly=False)

    # ------------------------------------------------------------------
    # Identity / External Link
    # ------------------------------------------------------------------

    x_external_source = fields.Char(string="External Source", default="geotab")
    x_geotab_device_id = fields.Char(string="Geotab Device ID", index=True)
    x_geotab_vehicle_id = fields.Char(string="Geotab Vehicle ID")
    x_last_eld_sync_at = fields.Datetime(string="Last ELD Sync")
    x_sync_status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("pending", "Pending")],
        string="Sync Status",
    )
    x_sync_error = fields.Text(string="Sync Error")

    # ------------------------------------------------------------------
    # Live Telematics
    # ------------------------------------------------------------------

    x_current_odometer_km = fields.Float(string="Odometer (km)", digits=(12, 1))
    x_current_engine_hours = fields.Float(string="Engine Hours", digits=(10, 1))
    x_current_fuel_percent = fields.Float(string="Fuel Level (%)", digits=(5, 1))
    x_current_fuel_liters = fields.Float(
        string="Fuel (L)",
        compute="_compute_fuel_liters",
        store=True,
        digits=(8, 1),
    )
    x_current_def_percent = fields.Float(string="DEF Level (%)", digits=(5, 1))
    x_current_def_liters = fields.Float(
        string="DEF (L)",
        compute="_compute_def_liters",
        store=True,
        digits=(8, 1),
    )
    x_estimated_range_km = fields.Float(
        string="Est. Range (km)",
        compute="_compute_estimated_range",
        store=True,
        digits=(8, 1),
    )
    x_last_location_lat = fields.Float(string="Last Lat", digits=(10, 6))
    x_last_location_lng = fields.Float(string="Last Lng", digits=(10, 6))
    x_last_location_at = fields.Datetime(string="Last Location Time")
    x_last_location_address = fields.Char(string="Last Known Address", readonly=True)
    x_last_valid_telematics_at = fields.Datetime(
        string="Last Valid Fuel/DEF Reading",
        help="Timestamp of the last sync that returned a non-zero fuel or DEF reading.",
    )

    # ------------------------------------------------------------------
    # Fuel / Efficiency
    # ------------------------------------------------------------------

    x_avg_km_per_l_last_week = fields.Float(string="Avg km/L", digits=(6, 2))
    x_avg_l_per_100km_last_week = fields.Float(string="Avg L/100km", digits=(6, 2))
    x_last_fuel_sync_at = fields.Datetime(string="Last Fuel Sync")

    # ------------------------------------------------------------------
    # Truck Config
    # ------------------------------------------------------------------

    x_tank_capacity_l = fields.Float(string="Tank Capacity (L)", digits=(8, 1))
    x_def_tank_capacity_l = fields.Float(string="DEF Tank Capacity (L)", digits=(8, 1))
    x_reserve_fuel_percent = fields.Float(string="Reserve Fuel (%)", default=25.0, digits=(5, 1))
    x_home_base_lat = fields.Float(string="Home Base Lat", digits=(10, 6))
    x_home_base_lng = fields.Float(string="Home Base Lng", digits=(10, 6))
    x_home_base_address = fields.Char(string="Home Base Address")
    x_reefer = fields.Boolean(string="Reefer Unit")
    x_liftgate = fields.Boolean(string="Liftgate")
    x_air_ride = fields.Boolean(string="Air Ride Suspension")
    x_max_pallets = fields.Integer(string="Max Pallets")
    x_max_payload_lbs = fields.Float(string="Max Payload (lbs)", digits=(10, 1))
    x_gvwr_lbs = fields.Float(string="GVWR (lbs)", digits=(10, 0))
    x_operational_logistics = fields.Boolean(
        string="Operational for Logistics",
        default=False,
        help="Only vehicles with this flag count toward phase activation and route-run assignment.",
    )
    x_vehicle_height_ft = fields.Float(string="Vehicle Height (ft)", digits=(10, 2))
    x_overall_length_ft = fields.Float(string="Overall Length (ft)", digits=(10, 2))
    x_cargo_box_length_ft = fields.Float(string="Cargo Box Length (ft)", digits=(10, 2))
    x_box_interior_height_ft = fields.Float(string="Box Interior Height (ft)", digits=(10, 2))
    x_door_clearance_in = fields.Float(string="Door Clearance (in)", digits=(10, 1))
    x_dock_height_in = fields.Float(string="Dock Height (in)", digits=(10, 1))

    # ------------------------------------------------------------------
    # Costing
    # ------------------------------------------------------------------

    x_monthly_maintenance_budget = fields.Float(string="Monthly Maintenance Budget", digits=(10, 2))
    x_maintenance_cost_per_km = fields.Float(
        string="Maintenance Cost/km",
        compute="_compute_maintenance_cost_per_km",
        store=True,
        digits=(8, 4),
    )
    x_monthly_insurance_budget = fields.Float(string="Monthly Insurance Budget", digits=(10, 2))
    x_insurance_cost_per_km = fields.Float(
        string="Insurance Cost/km",
        compute="_compute_insurance_cost_per_km",
        store=True,
        digits=(8, 4),
    )
    x_monthly_avg_km = fields.Float(
        string="Monthly Avg km (for costing)",
        help="Last 30 days distance from Geotab, or set manually for cost calculations.",
        digits=(10, 1),
    )
    x_driver_rate_per_hr = fields.Float(
        string="Driver Rate ($/hr)",
        digits=(6, 2),
        default=0.0,
        help=(
            "Hourly driver cost for rate estimation on this truck.\n"
            "0 = use the system default from Settings → Technical → System Parameters "
            "→ estimator.driver_rate_per_hr.\n"
            "Set a value here to override the system default for this truck only."
        ),
    )

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------

    x_current_driver_contact_id = fields.Many2one(
        "res.partner",
        string="Active Driver",
        domain=[("x_is_driver_profile", "=", True)],
    )

    # ------------------------------------------------------------------
    # Computed: fuel liters from percent * tank capacity
    # ------------------------------------------------------------------

    @api.depends("x_current_fuel_percent", "x_tank_capacity_l")
    def _compute_fuel_liters(self):
        for v in self:
            if v.x_tank_capacity_l and v.x_current_fuel_percent:
                v.x_current_fuel_liters = v.x_tank_capacity_l * (v.x_current_fuel_percent / 100.0)
            else:
                v.x_current_fuel_liters = 0.0

    @api.depends("x_current_def_percent", "x_def_tank_capacity_l")
    def _compute_def_liters(self):
        for v in self:
            if v.x_def_tank_capacity_l and v.x_current_def_percent:
                v.x_current_def_liters = v.x_def_tank_capacity_l * (v.x_current_def_percent / 100.0)
            else:
                v.x_current_def_liters = 0.0

    # ------------------------------------------------------------------
    # Computed: estimated range using reserve threshold and avg km/L
    # ------------------------------------------------------------------

    @api.depends(
        "x_current_fuel_percent",
        "x_tank_capacity_l",
        "x_reserve_fuel_percent",
        "x_avg_km_per_l_last_week",
    )
    def _compute_estimated_range(self):
        for v in self:
            tank = v.x_tank_capacity_l or 0.0
            fuel_pct = v.x_current_fuel_percent or 0.0
            reserve_pct = v.x_reserve_fuel_percent or 25.0
            avg_km_l = v.x_avg_km_per_l_last_week or 0.0

            if not tank or not avg_km_l:
                v.x_estimated_range_km = 0.0
                continue

            current_liters = tank * (fuel_pct / 100.0)
            reserve_liters = tank * (reserve_pct / 100.0)
            usable = max(0.0, current_liters - reserve_liters)
            v.x_estimated_range_km = usable * avg_km_l

    # ------------------------------------------------------------------
    # Computed: cost per km from monthly budgets
    # ------------------------------------------------------------------

    @api.depends("x_monthly_maintenance_budget", "x_monthly_avg_km")
    def _compute_maintenance_cost_per_km(self):
        for v in self:
            if v.x_monthly_avg_km and v.x_monthly_maintenance_budget:
                v.x_maintenance_cost_per_km = v.x_monthly_maintenance_budget / v.x_monthly_avg_km
            else:
                v.x_maintenance_cost_per_km = 0.0

    @api.depends("x_monthly_insurance_budget", "x_monthly_avg_km")
    def _compute_insurance_cost_per_km(self):
        for v in self:
            if v.x_monthly_avg_km and v.x_monthly_insurance_budget:
                v.x_insurance_cost_per_km = v.x_monthly_insurance_budget / v.x_monthly_avg_km
            else:
                v.x_insurance_cost_per_km = 0.0

    # ------------------------------------------------------------------
    # One2many back-relation for assignment history (used in view)
    # ------------------------------------------------------------------

    driver_assignment_ids = fields.One2many(
        "fleet.driver.assignment",
        "vehicle_id",
        string="Driver Assignments",
    )

    daily_odometer_ids = fields.One2many(
        "fleet.daily.odometer",
        "vehicle_id",
        string="Daily Odometer Log",
    )

    # ------------------------------------------------------------------
    # Geotab link / re-sync actions
    # ------------------------------------------------------------------

    def action_open_geotab_link_wizard(self):
        """Open the wizard to select a Geotab device and import data."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Link to Geotab Device",
            "res_model": "fleet.geotab.link.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_vehicle_id": self.id},
        }

    def action_resync_from_geotab(self):
        """Re-import all live Geotab data for this vehicle."""
        self.ensure_one()
        if not self.x_geotab_device_id:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No Geotab Link",
                    "message": "This vehicle is not linked to a Geotab device yet.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        try:
            self.env["premafirm.geotab.sync"].import_vehicle_data(self)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Sync Complete",
                    "message": f"Live data imported from Geotab for {self.name}.",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as exc:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Sync Failed",
                    "message": str(exc),
                    "type": "danger",
                    "sticky": True,
                },
            }

    # ------------------------------------------------------------------
    # Fuel averages sync action (manual button on Geotab tab)
    # ------------------------------------------------------------------

    def action_sync_fuel_averages(self):
        """Re-compute monthly fuel efficiency averages from Geotab trips for this vehicle."""
        self.ensure_one()
        if not self.x_geotab_device_id:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No Geotab Link",
                    "message": "This vehicle is not linked to a Geotab device yet.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        from datetime import datetime, timezone
        from ..services.geotab_service import GeotabService
        from .geotab_sync import _fuel_period_dates, _compute_fuel_avg
        try:
            svc = GeotabService(self.env)
            now_utc = datetime.now(timezone.utc)
            from_date, to_date, period_label = _fuel_period_dates(now_utc)
            avg_km_l, avg_l_100km = _compute_fuel_avg(
                svc, self.x_geotab_device_id, from_date, to_date, self.name,
                tank_capacity_l=self.x_tank_capacity_l or 0.0,
            )
            if avg_km_l is None:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "No Fuel Data",
                        "message": (
                            f"No usable fuel data from Geotab for {period_label}. "
                            "This vehicle uses fuel-level sensor data — make sure "
                            "Tank Capacity (L) is set on the Truck Config tab, then retry."
                        ),
                        "type": "warning",
                        "sticky": True,
                    },
                }
            self.write({
                "x_avg_km_per_l_last_week": avg_km_l,
                "x_avg_l_per_100km_last_week": avg_l_100km,
                "x_last_fuel_sync_at": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": f"Fuel Efficiency Updated ({period_label})",
                    "message": f"{avg_km_l:.2f} km/L  |  {avg_l_100km:.2f} L/100km",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as exc:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Sync Failed",
                    "message": str(exc),
                    "type": "danger",
                    "sticky": True,
                },
            }

    # ------------------------------------------------------------------
    # Geotab fuel diagnostic (dev button — logs raw API responses)
    # ------------------------------------------------------------------

    def action_diagnose_geotab_fuel(self):
        """Query Geotab raw data for this vehicle and write full diagnostic to the Odoo log."""
        self.ensure_one()
        if not self.x_geotab_device_id:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "No Geotab Link", "message": "No device ID.", "type": "warning"}}
        from datetime import datetime, timezone
        from ..services.geotab_service import GeotabService, DIAG_TOTAL_FUEL_USED
        import logging
        _diag = logging.getLogger(__name__)
        try:
            svc = GeotabService(self.env)
            d_id = self.x_geotab_device_id
            now = datetime.now(timezone.utc)
            from_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            lines = [f"=== FUEL DIAGNOSTIC: {self.name} (device={d_id}) ==="]

            # Trips
            trips = svc.get_trips(d_id, from_date=from_date, to_date=now)
            lines.append(f"Trips Apr 1→now: {len(trips)}")
            if trips:
                t0 = trips[0]
                lines.append(f"Trip[0] keys: {list(t0.keys())}")
                lines.append(f"Trip[0] distance={t0.get('distance')}  fuelConsumed={t0.get('fuelConsumed')}  averageFuelEconomy={t0.get('averageFuelEconomy')}")
                total_raw = sum(float(x.get("distance") or 0) for x in trips)
                total_fc = sum(float(x.get("fuelConsumed") or 0) for x in trips)
                total_eco = sum(float(x.get("averageFuelEconomy") or 0) for x in trips)
                lines.append(f"Sum: raw_distance={total_raw}  fuelConsumed={total_fc}  averageFuelEconomy(sum)={total_eco}")

            # Total fuel used counter
            rs = svc.get_status_data(d_id, DIAG_TOTAL_FUEL_USED, to_date=from_date)
            re = svc.get_status_data(d_id, DIAG_TOTAL_FUEL_USED, to_date=now)
            lines.append(f"DiagnosticEngineTotalFuelUsedId: start={rs}  end={re}")

            # FuelTransaction — all in DB (no filter)
            all_ft = svc.call("Get", type_name="FuelTransaction", search={}) or []
            lines.append(f"FuelTransaction ALL (no filter): {len(all_ft)} records")
            if all_ft:
                dev_ids = list({(t.get("device") or {}).get("id", "?") for t in all_ft})
                lines.append(f"Device IDs in all FuelTransactions: {dev_ids[:15]}")
                lines.append(f"FuelTransaction[0]: {all_ft[0]}")

            # FuelTransaction with date + device filter
            ft_dev = svc.get_fuel_transactions(d_id, from_date=from_date, to_date=now)
            lines.append(f"FuelTransaction (device={d_id}, Apr MTD): {len(ft_dev)} records")
            if ft_dev:
                lines.append(f"FuelTransaction[0]: {ft_dev[0]}")

            msg = "\n".join(lines)
            _diag.info(msg)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Fuel Diagnostic — see Odoo log",
                    "message": f"Trips: {len(trips)} | FuelTxn total: {len(all_ft)} | FuelTxn device: {len(ft_dev)} — full dump in /var/log/odoo/odoo18.log",
                    "type": "info",
                    "sticky": True,
                },
            }
        except Exception as exc:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Diagnostic Failed", "message": str(exc), "type": "danger", "sticky": True}}

    # ------------------------------------------------------------------
    # Daily odometer import action (manual sync button)
    # ------------------------------------------------------------------

    def action_sync_daily_odometer(self):
        """Manually import the last 7 days of odometer + engine hours + driver from Geotab."""
        self.ensure_one()
        if not self.x_geotab_device_id:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No Geotab Link",
                    "message": "This vehicle is not linked to a Geotab device.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        try:
            self.env["premafirm.geotab.sync"].import_daily_odometer(self)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Complete",
                    "message": "Last 7 days of odometer data imported from Geotab.",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as exc:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": str(exc),
                    "type": "danger",
                    "sticky": True,
                },
            }

    # ------------------------------------------------------------------
    # Driver assignment helper
    # ------------------------------------------------------------------

    def set_active_driver(self, driver_contact, source="manual", notes=""):
        self.ensure_one()
        Assignment = self.env["fleet.driver.assignment"]
        now = fields.Datetime.now()
        # Close any currently open assignment for this vehicle
        open_assignments = Assignment.search([
            ("vehicle_id", "=", self.id),
            ("active", "=", True),
            ("end_datetime", "=", False),
        ])
        open_assignments.write({"end_datetime": now, "active": False})
        # Update truck driver field
        self.x_current_driver_contact_id = driver_contact.id if driver_contact else False
        if driver_contact:
            Assignment.create({
                "vehicle_id": self.id,
                "driver_contact_id": driver_contact.id,
                "start_datetime": now,
                "source": source,
                "notes": notes,
                "active": True,
            })
