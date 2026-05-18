"""
Trip cost estimate model — RPC entry point and persisted audit log.

Frontend calls calculate_estimate() and geocode_address_rpc() via orm.call().
Each successful calculation creates a record for historical reference.
"""
import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


class PremafirmEstimatorTruckPref(models.Model):
    """Per-truck saved cost defaults — auto-loaded when truck is selected."""
    _name = "premafirm.estimator.truck.pref"
    _description = "Estimator Truck Preferences"

    vehicle_id          = fields.Many2one("fleet.vehicle", required=True,
                                          ondelete="cascade", index=True)
    driver_rate_per_hr  = fields.Float(digits=(6, 2))
    maintenance_monthly = fields.Float(digits=(10, 2))
    insurance_monthly   = fields.Float(digits=(10, 2))
    fuel_price_per_l    = fields.Float(digits=(6, 4))
    margin_pct          = fields.Float(digits=(5, 1))


class PremafirmRateEstimator(models.Model):
    _name = "premafirm.rate.estimator"
    _description = "Trip Cost Estimate"
    _order = "create_date desc"

    # ── Inputs ─────────────────────────────────────────────────────────

    vehicle_id        = fields.Many2one("fleet.vehicle", string="Truck", ondelete="set null")
    origin_address    = fields.Char(string="Origin")
    origin_lat        = fields.Float(digits=(10, 6))
    origin_lng        = fields.Float(digits=(10, 6))
    destination_address = fields.Char(string="Destination")
    destination_lat   = fields.Float(digits=(10, 6))
    destination_lng   = fields.Float(digits=(10, 6))
    allow_cross_border = fields.Boolean(string="Allow USA Routing", default=False)
    avoid_tolls       = fields.Boolean(string="Avoid Tolls", default=True)
    load_weight_lbs   = fields.Float(string="Load Weight (lbs)", digits=(10, 1))
    load_pallets      = fields.Integer(string="Pallets")
    scheduled_at      = fields.Datetime(string="Scheduled Pickup")
    notes             = fields.Text(string="Trip Notes / Instructions")
    x_route_sheet_text = fields.Text(
        string="Paste Route Sheet / Text Message",
        help="Paste any text — email, WhatsApp, rate sheet, or spoken description. "
             "Click 'Parse & Fill' to extract origin, destination, pallets, and weight.",
    )
    x_chat_message = fields.Text(string="Work Order / Message")
    x_chat_response = fields.Html(string="AI Estimate Response", readonly=True)
    source_invoice_id = fields.Many2one("account.move", string="Source Invoice", readonly=True, ondelete="set null")

    # ── Fleetbase dispatch ─────────────────────────────────────────────

    fleetbase_order_id  = fields.Char(string="Fleetbase Order ID", readonly=True)
    fleetbase_tracking  = fields.Char(string="Tracking #", readonly=True)
    fleetbase_status    = fields.Char(string="Dispatch Status", readonly=True)
    fleetbase_url       = fields.Char(string="Tracking URL", readonly=True)

    # ── Route ──────────────────────────────────────────────────────────

    stops_json    = fields.Text(string="Stops (JSON)")
    stop_ids      = fields.One2many("premafirm.estimator.stop", "estimator_id",
                                    string="Stops")
    attachment_ids = fields.Many2many(
        "ir.attachment",
        string="Attachments",
        compute="_compute_attachment_ids",
        inverse="_inverse_attachment_ids",
    )
    attachment_count = fields.Integer(compute="_compute_attachment_ids")
    distance_km   = fields.Float(string="Distance (km)", digits=(10, 1))
    duration_hrs  = fields.Float(string="Drive Time (hrs)", digits=(6, 2))

    # ── Cost breakdown ─────────────────────────────────────────────────

    prev_month_km     = fields.Float(string="Prev Month km (basis)", digits=(10, 1))
    fuel_liters       = fields.Float(digits=(8, 2))
    fuel_load_factor  = fields.Float(string="Fuel Load Factor", digits=(6, 3))
    fuel_cost         = fields.Float(string="Fuel Cost", digits=(10, 2))
    maintenance_cost  = fields.Float(digits=(10, 2))
    insurance_cost    = fields.Float(digits=(10, 2))
    driver_cost       = fields.Float(digits=(10, 2))
    weight_surcharge  = fields.Float(string="Weight Surcharge", digits=(10, 2))
    total_cost        = fields.Float(digits=(10, 2))
    margin_pct        = fields.Float(string="Margin %", digits=(5, 1))
    suggested_rate    = fields.Float(digits=(10, 2))

    # ── Params used (for transparency) ─────────────────────────────────

    fuel_price_per_l   = fields.Float(digits=(6, 3))
    driver_rate_per_hr = fields.Float(digits=(6, 2))
    avg_km_per_l       = fields.Float(string="Avg km/L", digits=(6, 2))

    # ── Invoice / Job relationship ──────────────────────────────────────

    invoice_id        = fields.Many2one("account.move", string="Invoice",
                                        ondelete="set null", index=True)
    job_day_ref       = fields.Char(string="Job Day")
    job_sequence      = fields.Integer(string="Job #", default=1)
    calendar_event_id = fields.Many2one("calendar.event", string="Calendar Event",
                                        ondelete="set null", readonly=True)

    # ── Lifecycle ──────────────────────────────────────────────────────

    state         = fields.Selection(
        [("saved", "Saved"), ("error", "Error")], default="saved"
    )
    error_message = fields.Text()
    user_id       = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    name          = fields.Char(compute="_compute_name", store=True)

    @api.depends("vehicle_id", "destination_address", "create_date")
    def _compute_name(self):
        for rec in self:
            truck = rec.vehicle_id.name if rec.vehicle_id else "?"
            dest  = rec.destination_address or "?"
            if rec.create_date:
                date_str = rec.create_date.strftime("%b %d")
            else:
                date_str = ""
            rec.name = f"{truck} → {dest} ({date_str})"

    def _compute_attachment_ids(self):
        Attach = self.env["ir.attachment"].sudo()
        for rec in self:
            atts = Attach.search([("res_model", "=", "premafirm.rate.estimator"), ("res_id", "=", rec.id)])
            rec.attachment_ids = atts
            rec.attachment_count = len(atts)

    def _inverse_attachment_ids(self):
        # Handled by standard ir.attachment res_model/res_id binding — no-op needed here.
        pass

    # ── Stop helpers ──────────────────────────────────────────────────

    def _stops_to_json(self):
        """Serialize stop_ids to stops_json string."""
        if self.stop_ids:
            self.stops_json = json.dumps([s.as_dict() for s in self.stop_ids.sorted("sequence")])

    def _json_to_stops(self, stops_data=None):
        """Populate stop_ids from stops_json (or from passed list).  Existing stops are deleted."""
        if stops_data is None:
            try:
                stops_data = json.loads(self.stops_json or "[]")
            except Exception:
                return
        self.stop_ids.unlink()
        for i, s in enumerate(stops_data):
            sched = None
            if s.get("scheduled_time"):
                try:
                    from datetime import datetime as _dt
                    raw = str(s["scheduled_time"])
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
                        try:
                            sched = _dt.strptime(raw, fmt)
                            break
                        except Exception:
                            continue
                except Exception:
                    pass
            self.env["premafirm.estimator.stop"].create({
                "estimator_id":   self.id,
                "sequence":       (i + 1) * 10,
                "stop_type":      s.get("type") or s.get("stop_type") or "pickup",
                "is_system":      bool(s.get("is_system")),
                "name":           s.get("name") or "",
                "address":        s.get("address") or "",
                "place_id":       s.get("place_id") or "",
                "lat":            float(s.get("lat") or 0),
                "lng":            float(s.get("lng") or 0),
                "scheduled_time": sched,
                "notes":          s.get("notes") or "",
                "pallets":        int(s.get("pallets") or 0),
                "weight_lbs":     float(s.get("weight_lbs") or 0),
                "liftgate":       bool(s.get("liftgate")),
            })

    def add_system_stops(self):
        """Add origin and return system stops based on truck GPS / home base.

        Only adds stops if no existing system stop of that type is present.
        Never overwrites user-defined stops.
        """
        vehicle = self.vehicle_id
        if not vehicle:
            return

        lat = vehicle.x_last_location_lat or vehicle.x_home_base_lat or 0.0
        lng = vehicle.x_last_location_lng or vehicle.x_home_base_lng or 0.0
        address = (vehicle.x_last_location_address or vehicle.x_home_base_address
                   or vehicle.name or "")
        if not lat and not lng:
            # Try company address as last resort
            company = self.env.company
            lat = getattr(company, "partner_latitude", 0.0) or 0.0
            lng = getattr(company, "partner_longitude", 0.0) or 0.0
            address = company.name or ""

        Stop = self.env["premafirm.estimator.stop"]
        existing_types = self.stop_ids.mapped("stop_type")

        if "origin" not in existing_types:
            # Prepend origin before all existing stops
            min_seq = min(self.stop_ids.mapped("sequence") or [10])
            Stop.create({
                "estimator_id": self.id,
                "sequence": max(min_seq - 10, 0),
                "stop_type": "origin",
                "is_system": True,
                "address": address,
                "lat": lat,
                "lng": lng,
                "name": vehicle.name or "",
            })

        if "return" not in existing_types:
            max_seq = max(self.stop_ids.mapped("sequence") or [10])
            Stop.create({
                "estimator_id": self.id,
                "sequence": max_seq + 10,
                "stop_type": "return",
                "is_system": True,
                "address": address,
                "lat": lat,
                "lng": lng,
                "name": vehicle.name or "",
            })

        self._stops_to_json()

    @api.model
    def add_system_stops_rpc(self, estimate_id):
        """RPC wrapper — called from frontend after truck selection."""
        rec = self.sudo().browse(estimate_id)
        if not rec.exists():
            return {"error": "Estimate not found."}
        rec.add_system_stops()
        return {"success": True, "stop_count": len(rec.stop_ids)}

    # ── RPC: default cost params for the estimator form ───────────────

    @api.model
    def get_defaults_rpc(self, vehicle_id):
        """Return cost defaults for the estimator form.

        Priority: saved truck prefs → vehicle fields → system config.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        fuel_price  = float(ICP.get_param("estimator.fuel_price_per_l",   "1.55"))
        driver_rate = float(ICP.get_param("estimator.driver_rate_per_hr", "40.00"))
        margin_pct  = float(ICP.get_param("estimator.margin_pct",         "20.0"))

        insurance   = 0.0
        maintenance = 0.0
        avg_km_l    = 0.0

        if vehicle_id:
            vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
            if vehicle.exists():
                insurance   = vehicle.x_monthly_insurance_budget   or 0.0
                maintenance = vehicle.x_monthly_maintenance_budget or 0.0
                avg_km_l    = vehicle.x_avg_km_per_l_last_week     or 0.0

            # Saved truck prefs override system defaults
            pref = self.env["premafirm.estimator.truck.pref"].sudo().search(
                [("vehicle_id", "=", vehicle_id)], limit=1)
            if pref:
                if pref.fuel_price_per_l   > 0: fuel_price  = pref.fuel_price_per_l
                if pref.driver_rate_per_hr > 0: driver_rate = pref.driver_rate_per_hr
                if pref.insurance_monthly  > 0: insurance   = pref.insurance_monthly
                if pref.maintenance_monthly > 0: maintenance = pref.maintenance_monthly
                if pref.margin_pct         > 0: margin_pct  = pref.margin_pct

        return {
            "fuel_price_per_l":    round(fuel_price, 3),
            "driver_rate_per_hr":  round(driver_rate, 2),
            "insurance_monthly":   round(insurance, 2),
            "maintenance_monthly": round(maintenance, 2),
            "margin_pct":          round(margin_pct, 1),
            "avg_km_per_l":        round(avg_km_l, 2),
        }

    @api.model
    def save_truck_prefs_rpc(self, vehicle_id, prefs):
        """Persist user-entered cost params so they auto-load next time."""
        Pref = self.env["premafirm.estimator.truck.pref"].sudo()
        existing = Pref.search([("vehicle_id", "=", vehicle_id)], limit=1)
        vals = {
            "driver_rate_per_hr":  prefs.get("driver_rate_per_hr",  0.0) or 0.0,
            "maintenance_monthly": prefs.get("maintenance_monthly", 0.0) or 0.0,
            "insurance_monthly":   prefs.get("insurance_monthly",   0.0) or 0.0,
            "fuel_price_per_l":    prefs.get("fuel_price_per_l",    0.0) or 0.0,
            "margin_pct":          prefs.get("margin_pct",          0.0) or 0.0,
        }
        if existing:
            existing.write(vals)
        else:
            Pref.create({"vehicle_id": vehicle_id, **vals})
        return True

    # ── RPC: address autocomplete ───────────────────────────────────────

    @api.model
    def geocode_address_rpc(self, query):
        """Return address suggestions for the frontend autocomplete.

        Tries Google Geocoding API first (if google.maps.api.key is configured),
        then falls back to MapBox.
        """
        if not query or len(query) < 2:
            return []

        ICP = self.env["ir.config_parameter"].sudo()
        google_key = (
            ICP.get_param("google.maps.api.key")
            or ICP.get_param("google_maps_api_key")
            or ""
        )

        if google_key:
            try:
                url = (
                    "https://maps.googleapis.com/maps/api/geocode/json"
                    f"?address={urllib.parse.quote(query)}"
                    f"&key={google_key}"
                    "&components=country:CA|country:US"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "Odoo/18"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                results = []
                for item in (data.get("results") or [])[:5]:
                    loc = (item.get("geometry") or {}).get("location") or {}
                    if loc.get("lat") and loc.get("lng"):
                        results.append({
                            "place_name": item.get("formatted_address", ""),
                            "lat": loc["lat"],
                            "lng": loc["lng"],
                        })
                if results:
                    return results
            except Exception as e:
                _logger.warning("Google geocode failed for %r: %s", query, e)

        from ..services.mapbox_service import MapboxService
        try:
            return MapboxService(self.env).geocode_address(query)
        except Exception as e:
            _logger.warning("geocode_address_rpc MapBox error: %s", e)
            return []

    # ── Internal: geocode a single stop dict ───────────────────────────

    def _geocode_stop(self, stop, mbx):
        """Resolve a stop dict to lat/lng.

        stop: {type, address, lat, lng}
        Returns updated stop dict with resolved coordinates, or None if unresolvable.
        """
        lat = float(stop.get("lat") or 0)
        lng = float(stop.get("lng") or 0)
        address = (stop.get("address") or "").strip()

        if lat and lng:
            return {"type": stop.get("type", ""), "address": address or f"{lat},{lng}", "lat": lat, "lng": lng}

        if not address:
            return None

        hits = mbx.geocode_address(address)
        if not hits:
            return None
        return {"type": stop.get("type", ""), "address": hits[0]["place_name"], "lat": hits[0]["lat"], "lng": hits[0]["lng"]}

    def _vehicle_meets_requirements(self, vehicle, require_reefer=False, require_liftgate=False, load_pallets=0, load_weight_lbs=0.0):
        if require_reefer and not vehicle.x_reefer:
            return False
        if require_liftgate and not vehicle.x_liftgate:
            return False
        if load_pallets and vehicle.x_max_pallets and vehicle.x_max_pallets < load_pallets:
            return False
        if load_weight_lbs and vehicle.x_max_payload_lbs and vehicle.x_max_payload_lbs < load_weight_lbs:
            return False
        return True

    def _resolve_pickup_coords(self, stops):
        if not stops:
            return 0.0, 0.0, ""
        pickup = None
        for stop in stops:
            if (stop.get("type") or "").lower() == "pickup":
                pickup = stop
                break
        pickup = pickup or stops[0]
        lat = float(pickup.get("lat") or 0)
        lng = float(pickup.get("lng") or 0)
        address = (pickup.get("address") or "").strip()
        if lat and lng:
            return lat, lng, address
        if address:
            hits = self.geocode_address_rpc(address)
            if hits:
                return hits[0]["lat"], hits[0]["lng"], hits[0]["place_name"]
        return 0.0, 0.0, address

    def _parse_dt(self, value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        text = str(value).strip()
        if not text:
            return None
        candidates = [text, text.replace("Z", "+00:00")]
        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                return parsed
            except Exception:
                continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        return None

    def _extract_requested_window(self, scheduled_at_str, duration_hrs):
        scheduled_start = self._parse_dt(scheduled_at_str) or datetime.now()
        duration_hrs = float(duration_hrs or 8.0)
        if duration_hrs <= 0:
            duration_hrs = 8.0
        scheduled_end = scheduled_start + timedelta(hours=duration_hrs)
        return scheduled_start, scheduled_end

    def _intervals_overlap(self, start_a, end_a, start_b, end_b):
        if not start_a or not end_a or not start_b or not end_b:
            return False
        return max(start_a, start_b) < min(end_a, end_b)

    def _merge_intervals(self, intervals):
        cleaned = sorted(
            [(start, end) for start, end in intervals if start and end and end > start],
            key=lambda item: item[0],
        )
        if not cleaned:
            return []
        merged = [cleaned[0]]
        for start, end in cleaned[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return merged

    def _compute_free_windows(self, intervals, day_start, day_end, duration_hrs):
        merged = self._merge_intervals(intervals)
        cursor = day_start
        minimum = timedelta(hours=float(duration_hrs or 8.0))
        windows = []
        for start, end in merged:
            if start > cursor and (start - cursor) >= minimum:
                windows.append((cursor, start))
            cursor = max(cursor, end)
        if day_end > cursor and (day_end - cursor) >= minimum:
            windows.append((cursor, day_end))
        return windows

    def _format_slot(self, start, end, truck=None):
        label = f"{start.strftime('%a %b %d, %I:%M %p')} - {end.strftime('%I:%M %p')}"
        result = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": label,
        }
        if truck:
            result["truck_id"] = truck.id
            result["truck_name"] = truck.name or ""
        return result

    def _fleetbase_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param("fleetbase.enabled", "False")
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    # ── RPC: full estimate calculation ─────────────────────────────────

    @api.model
    def calculate_estimate(
        self,
        vehicle_id,
        stops,
        allow_cross_border=False,
        avoid_tolls=True,
        load_weight_lbs=0.0,
        load_pallets=0,
        fuel_price_per_l=0.0,
        driver_rate_per_hr=0.0,
        insurance_monthly=0.0,
        maintenance_monthly=0.0,
        margin_pct=0.0,
        scheduled_at=None,
        notes=None,
    ):
        """Main RPC called from the Prema AI frontend.

        stops: list of {type, address, lat, lng} — at least 2 with resolvable addresses.
               First stop's address may be blank; then the truck's GPS location is used.
        Returns a structured dict for frontend rendering.
        Never raises — errors are returned as {'error': '...'}.
        """
        from ..services.mapbox_service import MapboxService
        from ..services.pricing_engine import PricingEngine

        try:
            vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
            if not vehicle.exists():
                return {"error": f"Vehicle ID {vehicle_id} not found."}

            mbx = MapboxService(self.env)
            eng = PricingEngine(self.env)

            if not stops or len(stops) < 1:
                return {"error": "Please add at least two stops."}

            # --- Resolve each stop's coordinates ---
            resolved_stops = []
            for i, stop in enumerate(stops):
                lat = float(stop.get("lat") or 0)
                lng = float(stop.get("lng") or 0)
                address = (stop.get("address") or "").strip()

                if i == 0 and not lat and not lng and not address:
                    # Use vehicle GPS as first stop
                    lat = vehicle.x_last_location_lat or 0.0
                    lng = vehicle.x_last_location_lng or 0.0
                    address = vehicle.x_last_location_address or ""
                    if not lat and not lng:
                        lat = vehicle.x_home_base_lat or 0.0
                        lng = vehicle.x_home_base_lng or 0.0
                        address = vehicle.x_home_base_address or ""
                    if not lat and not lng:
                        return {
                            "error": (
                                "No origin location found for this truck. "
                                "Type an address in the first stop, or ensure GeoTab is synced."
                            )
                        }
                    resolved_stops.append({"type": stop.get("type", "pickup"), "address": address, "lat": lat, "lng": lng})
                    continue

                rs = self._geocode_stop({"type": stop.get("type", ""), "address": address, "lat": lat, "lng": lng}, mbx)
                if rs:
                    resolved_stops.append(rs)
                elif address:
                    return {"error": f"Could not locate stop {i+1}: '{address}'. Try a more specific address."}
                # else: blank stop with no address — silently skip

            if len(resolved_stops) < 2:
                return {"error": "Please enter addresses for at least 2 stops (origin + destination)."}

            # --- Route ---
            waypoints = [{"lng": s["lng"], "lat": s["lat"]} for s in resolved_stops]
            route = mbx.get_route_multi(
                waypoints,
                max_height_ft=vehicle.x_vehicle_height_ft or 0.0,
                gvwr_lbs=vehicle.x_gvwr_lbs or 0.0,
                allow_cross_border=allow_cross_border,
                avoid_tolls=avoid_tolls,
            )

            # --- Liftgate unloading time ---
            ICP = self.env["ir.config_parameter"].sudo()
            liftgate_min_per_pallet = float(
                ICP.get_param("estimator.liftgate_min_per_pallet", "4"))
            liftgate_minutes = sum(
                (s.get("pallets") or 0) * liftgate_min_per_pallet
                for s in stops
                if s.get("liftgate") and (s.get("pallets") or 0) > 0
            )
            liftgate_hrs = round(liftgate_minutes / 60.0, 2)
            total_duration_hrs = route["duration_hrs"] + liftgate_hrs

            # --- Costs ---
            cost_overrides = {
                "fuel_price_per_l":    fuel_price_per_l    or 0.0,
                "driver_rate_per_hr":  driver_rate_per_hr  or 0.0,
                "insurance_monthly":   insurance_monthly   or 0.0,
                "maintenance_monthly": maintenance_monthly or 0.0,
                "margin_pct":          margin_pct          or 0.0,
            }
            costs = eng.calculate(
                vehicle_id,
                route["distance_km"],
                total_duration_hrs,
                overrides=cost_overrides,
                load_weight_lbs=load_weight_lbs or 0.0,
            )

            resolved_origin = resolved_stops[0]["address"]
            resolved_dest = resolved_stops[-1]["address"]

            # --- Persist ---
            record = self.sudo().create({
                "vehicle_id":          vehicle_id,
                "origin_address":      resolved_origin,
                "origin_lat":          resolved_stops[0]["lat"],
                "origin_lng":          resolved_stops[0]["lng"],
                "destination_address": resolved_dest,
                "destination_lat":     resolved_stops[-1]["lat"],
                "destination_lng":     resolved_stops[-1]["lng"],
                "stops_json":          json.dumps(resolved_stops),
                "allow_cross_border":  allow_cross_border,
                "avoid_tolls":         avoid_tolls,
                "load_weight_lbs":     load_weight_lbs or 0.0,
                "load_pallets":        load_pallets or 0,
                "scheduled_at":        self._parse_dt(scheduled_at).strftime("%Y-%m-%d %H:%M:%S") if self._parse_dt(scheduled_at) else False,
                "notes":               notes or False,
                "distance_km":         route["distance_km"],
                "duration_hrs":        route["duration_hrs"],
                "prev_month_km":       costs["prev_month_km"],
                "fuel_liters":         costs["fuel_liters"],
                "fuel_load_factor":    costs["fuel_load_factor"],
                "fuel_cost":           costs["fuel_cost"],
                "maintenance_cost":    costs["maintenance_cost"],
                "insurance_cost":      costs["insurance_cost"],
                "driver_cost":         costs["driver_cost"],
                "weight_surcharge":    costs["weight_surcharge"],
                "total_cost":          costs["total_cost"],
                "margin_pct":          costs["margin_pct"],
                "suggested_rate":      costs["suggested_rate"],
                "fuel_price_per_l":    costs["fuel_price_per_l"],
                "driver_rate_per_hr":  costs["driver_rate_per_hr"],
                "avg_km_per_l":        costs["avg_km_per_l"],
                "state":               "saved",
            })
            record._json_to_stops(resolved_stops)

            return {
                "estimate_id": record.id,
                "route": {
                    "distance_km":        route["distance_km"],
                    "duration_hrs":       route["duration_hrs"],
                    "liftgate_hrs":       liftgate_hrs,
                    "total_duration_hrs": total_duration_hrs,
                    "origin_address":     resolved_origin,
                    "destination_address": resolved_dest,
                    "stop_count":         len(resolved_stops),
                    "stops":              resolved_stops,
                    "legs":               route.get("legs", []),
                },
                "costs": {
                    "fuel":             costs["fuel_cost"],
                    "maintenance":      costs["maintenance_cost"],
                    "insurance":        costs["insurance_cost"],
                    "driver":           costs["driver_cost"],
                    "weight_surcharge": costs["weight_surcharge"],
                    "total":            costs["total_cost"],
                    "suggested_rate":   costs["suggested_rate"],
                    "margin_pct":       costs["margin_pct"],
                },
                "params": {
                    "fuel_price_per_l":         costs["fuel_price_per_l"],
                    "driver_rate_per_hr":        costs["driver_rate_per_hr"],
                    "prev_month_km":             costs["prev_month_km"],
                    "avg_km_per_l":              costs["avg_km_per_l"],
                    "effective_km_per_l":        costs["effective_km_per_l"],
                    "fuel_load_factor":          costs["fuel_load_factor"],
                    "load_weight_lbs":           costs["load_weight_lbs"],
                    "weight_threshold_lbs":      costs["weight_threshold_lbs"],
                    "weight_surcharge_per_cwt":  costs["weight_surcharge_per_cwt"],
                },
                "truck": {
                    "id":            vehicle.id,
                    "name":          vehicle.name or "",
                    "license_plate": vehicle.license_plate or "",
                    "service_type":  getattr(vehicle, "service_type", "") or "dry",
                },
                "error": None,
            }

        except Exception as e:
            _logger.error("calculate_estimate error: %s", e, exc_info=True)
            return {"error": str(e)}

    # ── RPC: check truck availability ──────────────────────────────────

    @api.model
    def check_truck_availability_rpc(self, pickup_lat, pickup_lng, scheduled_at_str, duration_hrs=8.0):
        """Return availability status for all active trucks.

        For each truck, returns:
          - status: 'available' | 'busy' | 'unknown'
          - distance_km: straight-line km from truck GPS to pickup
          - next_available: ISO datetime string if busy

        If all trucks are busy, also returns suggested_slots (list of {start, end} ISO strings).
        """
        from ..services.fleetbase_service import _haversine_km
        from ..services.fleetbase_service import FleetbaseService
        try:
            pickup_lat = float(pickup_lat or 0)
            pickup_lng = float(pickup_lng or 0)
            duration_hrs = float(duration_hrs or 8)
            scheduled_start, scheduled_end = self._extract_requested_window(scheduled_at_str, duration_hrs)
            day_start = scheduled_start.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

            vehicles = self.env["fleet.vehicle"].sudo().search([
                ("active", "=", True),
            ])

            schedule_by_vehicle = {}
            schedule_lookup_error = ""
            if self._fleetbase_enabled():
                try:
                    fb_jobs = FleetbaseService(self.env).get_scheduled_jobs(
                        start=day_start - timedelta(hours=6),
                        end=day_end + timedelta(hours=6),
                    )
                    if fb_jobs:
                        by_id = {str(v.id): v.id for v in vehicles}
                        by_name = {str(v.name or "").strip().lower(): v.id for v in vehicles if v.name}
                        by_plate = {str(v.license_plate or "").strip().lower(): v.id for v in vehicles if v.license_plate}
                        for job in fb_jobs:
                            vehicle_ref_id = str(job.get("vehicle_id") or "").strip()
                            vehicle_ref_name = str(job.get("vehicle_name") or "").strip().lower()
                            vehicle_id = by_id.get(vehicle_ref_id)
                            if not vehicle_id and vehicle_ref_name:
                                vehicle_id = by_name.get(vehicle_ref_name) or by_plate.get(vehicle_ref_name)
                            if not vehicle_id:
                                continue
                            schedule_by_vehicle.setdefault(vehicle_id, []).append(job)
                except Exception as e:
                    schedule_lookup_error = str(e)
                    _logger.info("Fleetbase scheduler availability fallback engaged: %s", e)

            results = []
            suggested_slots = []
            for v in vehicles:
                truck_lat = v.x_last_location_lat or v.x_home_base_lat or 0.0
                truck_lng = v.x_last_location_lng or v.x_home_base_lng or 0.0

                dist_km = None
                if pickup_lat and pickup_lng and truck_lat and truck_lng:
                    dist_km = round(_haversine_km(pickup_lat, pickup_lng, truck_lat, truck_lng), 1)

                # Duty status from Geotab/driver
                driver_status = (v.x_current_driver_contact_id.x_driver_status or "") if v.x_current_driver_contact_id else ""
                truck_jobs = sorted(
                    schedule_by_vehicle.get(v.id, []),
                    key=lambda job: job.get("start") or scheduled_start,
                )
                same_day_intervals = []
                overlapping_jobs = []
                next_available = None
                for job in truck_jobs:
                    start = job.get("start")
                    end = job.get("end")
                    if not start or not end:
                        continue
                    same_day_intervals.append((start, end))
                    if self._intervals_overlap(start, end, scheduled_start, scheduled_end):
                        overlapping_jobs.append(job)
                        next_available = max(next_available, end) if next_available else end

                availability_windows = [
                    self._format_slot(start, end, truck=v)
                    for start, end in self._compute_free_windows(same_day_intervals, day_start, day_end, duration_hrs)[:3]
                ]

                if overlapping_jobs:
                    status = "busy"
                elif driver_status in ("on_duty_driving", "on_duty_not_driving"):
                    status = "busy"
                elif driver_status in ("off_duty", "sleeper_berth", ""):
                    status = "available"
                else:
                    status = "unknown"

                result = {
                    "id": v.id,
                    "name": v.name or "",
                    "license_plate": v.license_plate or "",
                    "status": status,
                    "driver_status": driver_status,
                    "distance_km": dist_km,
                    "truck_lat": truck_lat,
                    "truck_lng": truck_lng,
                    "driver_name": v.x_current_driver_contact_id.name if v.x_current_driver_contact_id else "",
                    "reefer": v.x_reefer or False,
                    "liftgate": v.x_liftgate or False,
                    "scheduled_jobs": len(truck_jobs),
                    "next_available": next_available.isoformat() if next_available else "",
                    "availability_windows": availability_windows,
                }
                if overlapping_jobs:
                    result["conflicting_jobs"] = [
                        {
                            "id": job.get("id", ""),
                            "label": job.get("label", ""),
                            "tracking_number": job.get("tracking_number", ""),
                            "start": job.get("start").isoformat() if job.get("start") else "",
                            "end": job.get("end").isoformat() if job.get("end") else "",
                        }
                        for job in overlapping_jobs[:5]
                    ]
                results.append(result)
                for slot in availability_windows[:2]:
                    suggested_slots.append(slot)

            # Sort: available first, then by distance
            def sort_key(r):
                order = 0 if r["status"] == "available" else (1 if r["status"] == "unknown" else 2)
                dist = r["distance_km"] if r["distance_km"] is not None else 99999
                return (order, dist)

            results.sort(key=sort_key)

            all_busy = all(r["status"] == "busy" for r in results) if results else False

            if all_busy:
                if suggested_slots:
                    suggested_slots = sorted(suggested_slots, key=lambda slot: slot.get("start", ""))[:6]
                else:
                    # Fallback if no Fleetbase schedule windows were available.
                    base = (scheduled_start + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
                    for offset_h in [0, 4, 8]:
                        slot_start = base + timedelta(hours=offset_h)
                        slot_end = slot_start + timedelta(hours=duration_hrs)
                        suggested_slots.append(self._format_slot(slot_start, slot_end))

            return {
                "trucks": results,
                "all_busy": all_busy,
                "suggested_slots": suggested_slots,
                "scheduled_start": scheduled_start.isoformat(),
                "scheduled_end": scheduled_end.isoformat(),
                "fleetbase_schedule_used": bool(schedule_by_vehicle),
                "schedule_lookup_error": schedule_lookup_error,
            }
        except Exception as e:
            _logger.error("check_truck_availability_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    # ── RPC: create Fleetbase dispatch order ───────────────────────────

    @api.model
    def create_fleetbase_job_rpc(self, estimate_id, stops, scheduled_at=None, notes=None, vehicle_id=None, order_number=None):
        """Create a dispatch order in Fleetbase and update the estimate record.

        stops: list of stop dicts with full time-window and address data.
        Returns: {success, order_id, tracking_number, public_url} or {error}.
        """
        from ..services.fleetbase_service import FleetbaseService
        try:
            fb = FleetbaseService(self.env)

            vehicle_name = None
            if vehicle_id:
                v = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
                if v.exists():
                    vehicle_name = v.name

            order = fb.create_order(
                stops=stops,
                scheduled_at=scheduled_at,
                notes=notes,
                vehicle_name=vehicle_name,
                order_number=order_number or None,
            )

            if estimate_id:
                try:
                    rec = self.sudo().browse(estimate_id)
                    if rec.exists():
                        rec.write({
                            "fleetbase_order_id": order.get("id", ""),
                            "fleetbase_tracking":  order.get("tracking_number", ""),
                            "fleetbase_status":    order.get("status", ""),
                            "fleetbase_url":       order.get("public_url", ""),
                        })
                except Exception as link_err:
                    _logger.warning("Could not link Fleetbase order to estimate: %s", link_err)

            return {
                "success": True,
                "order_id": order.get("id", ""),
                "tracking_number": order.get("tracking_number", ""),
                "status": order.get("status", ""),
                "public_url": order.get("public_url", ""),
            }
        except Exception as e:
            _logger.error("create_fleetbase_job_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    @api.model
    def suggest_dispatch_plan_rpc(
        self,
        stops,
        scheduled_at=None,
        vehicle_id=None,
        allow_cross_border=False,
        avoid_tolls=True,
        load_weight_lbs=0.0,
        load_pallets=0,
        notes=None,
        require_reefer=False,
        require_liftgate=False,
    ):
        """Choose the best truck for a structured stop list and build an estimate."""
        try:
            if not stops or len(stops) < 2:
                return {"error": "At least two stops are required to build a dispatch plan."}

            pickup_lat, pickup_lng, pickup_address = self._resolve_pickup_coords(stops)
            availability = self.check_truck_availability_rpc(
                pickup_lat,
                pickup_lng,
                scheduled_at,
                duration_hrs=8.0,
            )
            if availability.get("error"):
                return availability

            vehicle_model = self.env["fleet.vehicle"].sudo()
            manual_vehicle = vehicle_model.browse(vehicle_id).exists() if vehicle_id else vehicle_model
            candidates = []
            for truck_info in availability.get("trucks", []):
                truck = vehicle_model.browse(truck_info["id"]).exists()
                if not truck:
                    continue
                if manual_vehicle and vehicle_id and truck.id != manual_vehicle.id:
                    continue
                if not self._vehicle_meets_requirements(
                    truck,
                    require_reefer=require_reefer,
                    require_liftgate=require_liftgate,
                    load_pallets=load_pallets,
                    load_weight_lbs=load_weight_lbs,
                ):
                    continue
                if truck_info["status"] == "busy":
                    continue
                score = (
                    0 if truck_info["status"] == "available" else (1 if truck_info["status"] == "unknown" else 2),
                    truck_info["distance_km"] if truck_info["distance_km"] is not None else 99999,
                )
                candidates.append((score, truck, truck_info))

            if not candidates:
                suggested = availability.get("suggested_slots", [])
                if manual_vehicle and vehicle_id:
                    manual_info = next(
                        (truck_info for truck_info in availability.get("trucks", []) if truck_info["id"] == manual_vehicle.id),
                        {},
                    )
                    suggested = manual_info.get("availability_windows") or suggested
                return {
                    "error": "No suitable truck is free in the requested time window.",
                    "all_busy": availability.get("all_busy", False),
                    "suggested_slots": suggested,
                    "availability": {
                        "all_busy": availability.get("all_busy", False),
                        "suggested_slots": suggested,
                        "scheduled_start": availability.get("scheduled_start"),
                        "scheduled_end": availability.get("scheduled_end"),
                    },
                }

            candidates.sort(key=lambda item: item[0])
            best_score, best_truck, best_info = candidates[0]
            defaults = self.get_defaults_rpc(best_truck.id)
            estimate = self.calculate_estimate(
                best_truck.id,
                stops,
                allow_cross_border=allow_cross_border,
                avoid_tolls=avoid_tolls,
                load_weight_lbs=load_weight_lbs,
                load_pallets=load_pallets,
                fuel_price_per_l=defaults.get("fuel_price_per_l", 0),
                driver_rate_per_hr=defaults.get("driver_rate_per_hr", 0),
                insurance_monthly=defaults.get("insurance_monthly", 0),
                maintenance_monthly=defaults.get("maintenance_monthly", 0),
                margin_pct=defaults.get("margin_pct", 0),
                scheduled_at=scheduled_at,
                notes=notes,
            )
            if estimate.get("error"):
                return estimate

            ranked = []
            for _, truck, truck_info in candidates[:10]:
                ranked.append({
                    "id": truck.id,
                    "name": truck.name or "",
                    "license_plate": truck.license_plate or "",
                    "status": truck_info["status"],
                    "distance_km": truck_info["distance_km"],
                    "reefer": bool(truck.x_reefer),
                    "liftgate": bool(truck.x_liftgate),
                    "max_pallets": truck.x_max_pallets or 0,
                    "max_payload_lbs": truck.x_max_payload_lbs or 0,
                    "driver_name": truck_info.get("driver_name", ""),
                    "availability_windows": truck_info.get("availability_windows", []),
                })

            return {
                "success": True,
                "pickup_address": pickup_address,
                "selected_truck": ranked[0],
                "truck_candidates": ranked,
                "availability": {
                    "all_busy": availability.get("all_busy", False),
                    "suggested_slots": availability.get("suggested_slots", []),
                    "scheduled_start": availability.get("scheduled_start"),
                    "scheduled_end": availability.get("scheduled_end"),
                },
                "estimate": estimate,
            }
        except Exception as e:
            _logger.error("suggest_dispatch_plan_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    @api.model
    def estimate_and_dispatch_rpc(
        self,
        stops,
        scheduled_at=None,
        vehicle_id=None,
        allow_cross_border=False,
        avoid_tolls=True,
        load_weight_lbs=0.0,
        load_pallets=0,
        notes=None,
        require_reefer=False,
        require_liftgate=False,
        create_job=False,
    ):
        """One entry point for estimate + truck selection + optional Fleetbase job creation."""
        plan = self.suggest_dispatch_plan_rpc(
            stops=stops,
            scheduled_at=scheduled_at,
            vehicle_id=vehicle_id,
            allow_cross_border=allow_cross_border,
            avoid_tolls=avoid_tolls,
            load_weight_lbs=load_weight_lbs,
            load_pallets=load_pallets,
            notes=notes,
            require_reefer=require_reefer,
            require_liftgate=require_liftgate,
        )
        if plan.get("error") or not create_job:
            return plan

        if plan.get("availability", {}).get("all_busy"):
            return {
                **plan,
                "error": "All suitable trucks are currently busy. Review suggested slots before dispatching.",
            }

        selected_truck = plan.get("selected_truck") or {}
        estimate = plan.get("estimate") or {}
        dispatch = self.create_fleetbase_job_rpc(
            estimate_id=estimate.get("estimate_id"),
            stops=stops,
            scheduled_at=scheduled_at,
            notes=notes,
            vehicle_id=selected_truck.get("id"),
        )
        return {
            **plan,
            "dispatch": dispatch,
        }

    # ── RPC: extract stops from uploaded route sheet ───────────────────

    @api.model
    def extract_stops_from_file_rpc(self, file_b64, mimetype, filename, extra_notes=""):
        """Use AI to extract stop information from a route sheet (PDF/image).

        Returns: {stops: [...], notes, raw_text} or {error}.
        Each stop: {type, address, stop_date, time_window_start, time_window_end,
                    stop_notes, liftgate, pallets, company_name, po_number}
        """
        try:
            from ..services.dispatch_document_service import DispatchDocumentService

            parser = DispatchDocumentService(self.env)
            local_result = parser.parse_attachment(
                file_b64=file_b64,
                mimetype=mimetype,
                filename=filename,
                extra_notes=extra_notes,
            )
            local_stops = local_result.get("stops") or []
            local_confidence = float(local_result.get("confidence") or 0.0)
            if len(local_stops) >= 2 and local_confidence >= 0.7:
                for stop in local_stops:
                    addr = (stop.get("address") or "").strip()
                    if addr and (not stop.get("lat") or not stop.get("lng")):
                        try:
                            hits = self.geocode_address_rpc(addr)
                            if hits:
                                stop["address"] = hits[0]["place_name"]
                                stop["lat"] = hits[0]["lat"]
                                stop["lng"] = hits[0]["lng"]
                        except Exception:
                            _logger.info("Geocode failed for locally parsed stop %s", addr, exc_info=True)
                    stop.setdefault("lat", 0)
                    stop.setdefault("lng", 0)
                    stop["type"] = "delivery" if (stop.get("type") or "").lower() == "dropoff" else (stop.get("type") or "delivery")
                return {
                    "stops": local_stops,
                    "notes": local_result.get("notes", ""),
                    "raw_text": local_result.get("raw_text", "")[:500],
                    "used_vision": False,
                    "used_ai": False,
                    "document_type": local_result.get("document_type", "unknown"),
                    "confidence": local_confidence,
                    "parser_used": local_result.get("parser_used", "local"),
                }

            ICP = self.env["ir.config_parameter"].sudo()
            openai_key = (
                ICP.get_param("openai.api_key")
                or ICP.get_param("prema_ai.api_key")
                or ""
            )
            if not openai_key:
                return {"error": "AI API key not configured. Set openai.api_key in System Parameters."}

            from ..services.openai_utils import openai_chat as _claude_chat, DEFAULT_MODEL as _CLAUDE_DEFAULT
            import json as _json
            raw_text = local_result.get("raw_text", "")
            used_vision = False

            system_prompt = (
                "You are a freight dispatch assistant. Extract all pickup and delivery stops "
                "from the route sheet provided. Return ONLY a JSON object with this structure:\n"
                '{"stops": [{"type": "pickup|dropoff", "company_name": "...", "address": "full street address, city, province/state, postal code", '
                '"stop_date": "YYYY-MM-DD or empty", "time_window_start": "HH:MM or empty", "time_window_end": "HH:MM or empty", '
                '"po_number": "...", "stop_notes": "...", "liftgate": true|false, "pallets": 0, "weight_lbs": 0}], '
                '"notes": "any general trip notes or instructions"}\n'
                "If a field is unknown, use empty string or 0. Stops must be in route order."
            )

            ICP = self.env["ir.config_parameter"].sudo()
            claude_key = openai_key
            claude_model = ICP.get_param("prema_ai.fast_model") or _CLAUDE_DEFAULT

            if raw_text and len(raw_text) >= 100:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Route sheet content:\n\n{raw_text[:8000]}\n\nExtra notes from dispatcher: {extra_notes}"},
                ]
            else:
                # Vision path — Claude only accepts JPEG/PNG/GIF/WebP, NOT PDF
                used_vision = True
                vision_b64 = file_b64
                vision_mime = mimetype

                if mimetype in ("application/pdf", "pdf"):
                    # Convert first PDF page to JPEG for vision
                    try:
                        import base64 as _b64
                        from pdf2image import convert_from_bytes
                        raw_pdf = _b64.b64decode(file_b64)
                        pages = convert_from_bytes(raw_pdf, first_page=1, last_page=2,
                                                   dpi=150, fmt="jpeg")
                        if pages:
                            import io as _io
                            buf = _io.BytesIO()
                            pages[0].save(buf, format="JPEG", quality=85)
                            vision_b64 = _b64.b64encode(buf.getvalue()).decode("utf-8")
                            vision_mime = "image/jpeg"
                        else:
                            return {"error": "Could not render PDF to image for analysis."}
                    except Exception as pdf_err:
                        _logger.warning("PDF→JPEG conversion failed: %s", pdf_err)
                        return {"error": f"PDF vision conversion failed: {pdf_err}. Try uploading a JPEG image instead."}

                image_url = f"data:{vision_mime};base64,{vision_b64}"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                        {"type": "text", "text": f"Extract all stops from this route sheet. Extra notes: {extra_notes}"},
                    ]},
                ]

            content = _claude_chat(
                messages=messages,
                max_tokens=2000,
                api_key=claude_key,
                model=claude_model,
                timeout=90,
            )
            # Robust JSON extraction (handles markdown fences from Claude)
            content = content.strip()
            # Try direct parse, then fence strip, then brace-match
            parsed = None
            try:
                parsed = _json.loads(content)
            except Exception:
                pass
            if parsed is None:
                fence = re.sub(r'^```(?:json)?\s*', '', content, flags=re.IGNORECASE)
                fence = re.sub(r'\s*```\s*$', '', fence)
                try:
                    parsed = _json.loads(fence)
                except Exception:
                    pass
            if parsed is None:
                # brace-match fallback
                start = content.find('{')
                if start != -1:
                    depth = 0
                    in_str = False
                    esc = False
                    for ci, ch in enumerate(content[start:], start):
                        if esc: esc = False; continue
                        if ch == '\\' and in_str: esc = True; continue
                        if ch == '"': in_str = not in_str; continue
                        if in_str: continue
                        if ch == '{': depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                try: parsed = _json.loads(content[start:ci+1])
                                except Exception: pass
                                break
            if parsed is None:
                return {"error": f"AI could not return valid JSON. Raw: {content[:200]}"}
            parsed = parsed  # already a dict
            stops = parsed.get("stops") or []
            trip_notes = parsed.get("notes") or ""

            # Geocode each stop address
            for stop in stops:
                addr = (stop.get("address") or "").strip()
                if addr:
                    try:
                        hits = self.geocode_address_rpc(addr)
                        if hits:
                            stop["address"] = hits[0]["place_name"]
                            stop["lat"] = hits[0]["lat"]
                            stop["lng"] = hits[0]["lng"]
                    except Exception:
                        pass
                if "lat" not in stop:
                    stop["lat"] = 0
                if "lng" not in stop:
                    stop["lng"] = 0

            return {
                "stops": stops,
                "notes": trip_notes,
                "raw_text": raw_text[:500] if raw_text else "",
                "used_vision": used_vision,
                "used_ai": True,
                "document_type": local_result.get("document_type", "unknown"),
                "confidence": local_result.get("confidence", 0.0),
                "parser_used": "ai_fallback",
            }

        except Exception as e:
            _logger.error("extract_stops_from_file_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    @api.model
    def deserialize_stops(self, stops_json):
        if not stops_json:
            return []
        if isinstance(stops_json, list):
            return stops_json
        try:
            parsed = json.loads(stops_json)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    @api.model
    def infer_load_metrics(self, stops):
        stops = stops or []
        pickup_stops = [stop for stop in stops if (stop.get("type") or "").lower() == "pickup"]
        source = pickup_stops or stops
        pallets = 0
        weight_lbs = 0.0
        for stop in source:
            try:
                pallets += int(stop.get("pallets") or 0)
            except Exception:
                pass
            try:
                weight_lbs += float(stop.get("weight_lbs") or 0.0)
            except Exception:
                pass
        if not pickup_stops and stops:
            pallets = max([int(stop.get("pallets") or 0) for stop in stops] or [0])
            weight_lbs = max([float(stop.get("weight_lbs") or 0.0) for stop in stops] or [0.0])
        return {
            "load_pallets": pallets,
            "load_weight_lbs": round(weight_lbs, 1),
            "require_liftgate": any(bool(stop.get("liftgate")) for stop in stops),
        }

    @api.model
    def infer_scheduled_at(self, stops):
        stops = stops or []
        ordered = sorted(stops, key=lambda stop: 0 if (stop.get("type") or "").lower() == "pickup" else 1)
        for stop in ordered:
            stop_date = (stop.get("stop_date") or "").strip()
            start = (stop.get("time_window_start") or "").strip()
            if stop_date and start:
                parsed = self._parse_dt(f"{stop_date} {start}")
                if parsed:
                    return parsed.isoformat()
            if stop_date:
                parsed = self._parse_dt(stop_date)
                if parsed:
                    return parsed.replace(hour=8, minute=0, second=0, microsecond=0).isoformat()
        return ""

    def action_open_source_invoice(self):
        self.ensure_one()
        if not self.source_invoice_id:
            raise exceptions.UserError("No source invoice is linked to this estimate.")
        return {
            "type": "ir.actions.act_window",
            "name": "Source Invoice",
            "res_model": "account.move",
            "res_id": self.source_invoice_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_open_fleetbase_tracking(self):
        self.ensure_one()
        if not self.fleetbase_url:
            raise exceptions.UserError("No Fleetbase tracking URL is available on this estimate.")
        return {
            "type": "ir.actions.act_url",
            "url": self.fleetbase_url,
            "target": "new",
        }

    def action_dispatch_to_fleetbase(self):
        self.ensure_one()
        if self.fleetbase_order_id:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Already Dispatched",
                    "message": f"Fleetbase order already exists: {self.fleetbase_tracking or self.fleetbase_order_id}. Use 'Open Fleetbase' to view.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        if not self.vehicle_id:
            raise exceptions.UserError("Select a truck before dispatching.")
        if not self.scheduled_at:
            raise exceptions.UserError("Set a scheduled date/time before dispatching.")

        # Use stop_ids if populated, fall back to stops_json
        if self.stop_ids:
            stops = [s.as_dict() for s in self.stop_ids.sorted("sequence")]
        else:
            stops = self.deserialize_stops(self.stops_json)
        if len(stops) < 2:
            raise exceptions.UserError("At least two stops are required before dispatching.")
        # Validate coordinates on non-system stops
        for s in stops:
            if not s.get("is_system") and not (s.get("lat") and s.get("lng")):
                addr = s.get("address") or "unknown"
                raise exceptions.UserError(
                    f"Stop '{addr}' has no coordinates. Validate the address before dispatching."
                )

        scheduled_at = self.scheduled_at.isoformat() if self.scheduled_at else self.infer_scheduled_at(stops)
        pickup_lat, pickup_lng, _pickup_address = self._resolve_pickup_coords(stops)
        availability = self.check_truck_availability_rpc(
            pickup_lat,
            pickup_lng,
            scheduled_at,
            duration_hrs=self.duration_hrs or 8.0,
        )
        if availability.get("error"):
            raise exceptions.UserError(availability["error"])

        selected = next((truck for truck in availability.get("trucks", []) if truck["id"] == self.vehicle_id.id), None)
        if not selected:
            raise exceptions.UserError("Selected truck could not be checked for availability.")
        if selected.get("status") == "busy":
            windows = selected.get("availability_windows") or availability.get("suggested_slots") or []
            if windows:
                formatted = "\n".join(f"- {slot.get('label')}" for slot in windows[:5])
                raise exceptions.UserError(
                    "The selected truck is already scheduled in Fleetbase for that time.\n\n"
                    f"Available windows:\n{formatted}"
                )
            raise exceptions.UserError("The selected truck is already scheduled in Fleetbase for that time.")

        # Use invoice reference as the Fleetbase work order number (ref preferred, fall back to name)
        inv = self.invoice_id or self.source_invoice_id
        order_number = None
        if inv:
            order_number = inv.ref or inv.name or None

        dispatch = self.create_fleetbase_job_rpc(
            estimate_id=self.id,
            stops=stops,
            scheduled_at=scheduled_at,
            notes=self.notes,
            vehicle_id=self.vehicle_id.id,
            order_number=order_number,
        )
        if dispatch.get("error"):
            raise exceptions.UserError(dispatch["error"])
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Dispatched",
                "message": "Fleetbase order created successfully.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_open_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise exceptions.UserError("No invoice is linked to this job.")
        return {
            "type": "ir.actions.act_window",
            "name": "Invoice",
            "res_model": "account.move",
            "res_id": self.invoice_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_open_calendar_event(self):
        self.ensure_one()
        if not self.calendar_event_id:
            raise exceptions.UserError("No calendar event is linked to this job.")
        return {
            "type": "ir.actions.act_window",
            "name": "Calendar Event",
            "res_model": "calendar.event",
            "res_id": self.calendar_event_id.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_ai_rewrite_notes(self):
        """Open AI notes rewrite wizard."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Rewrite Notes (AI)",
            "res_model": "premafirm.notes.rewrite.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_estimator_id": self.id, "default_current_notes": self.notes or ""},
        }

    def action_ai_review_stops(self):
        """Open AI stops review wizard."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "AI Review Stops",
            "res_model": "premafirm.stops.review.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_estimator_id": self.id},
        }

    def action_parse_route_text(self):
        """Parse x_route_sheet_text → extract all stops → populate stop_ids + add home/return."""
        self.ensure_one()
        text = (self.x_route_sheet_text or '').strip()
        if not text:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'No text', 'message': 'Paste your route sheet text first.', 'type': 'warning'},
            }
        try:
            stops, notes = self._extract_stops_from_text(text)
            if not stops:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {'title': 'Nothing found', 'message': 'AI could not detect any stops. Check the text and try again.', 'type': 'warning'},
                }
            self.stop_ids.filtered(lambda s: not s.is_system).unlink()
            seq = 10
            for s in stops:
                self.env['premafirm.estimator.stop'].create({
                    'estimator_id': self.id,
                    'sequence': seq,
                    'stop_type': s.get('type', 'delivery'),
                    'name': s.get('company_name', ''),
                    'address': s.get('address', ''),
                    'lat': float(s.get('lat') or 0),
                    'lng': float(s.get('lng') or 0),
                    'pallets': int(s.get('pallets') or 0),
                    'weight_lbs': float(s.get('weight_lbs') or 0),
                    'liftgate': bool(s.get('liftgate')),
                    'notes': s.get('stop_notes', ''),
                })
                seq += 10
            if notes and not self.notes:
                self.notes = notes
            self.add_system_stops()
            total_pallets = sum(int(s.get('pallets') or 0) for s in stops if s.get('type') == 'pickup')
            if total_pallets:
                self.load_pallets = total_pallets
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Stops Extracted',
                    'message': f'{len(stops)} stops loaded (home + return added automatically). Review stops then run Calculate.',
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'},
                },
            }
        except Exception as exc:
            _logger.error("action_parse_route_text error: %s", exc, exc_info=True)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'Parse Error', 'message': str(exc), 'type': 'danger'},
            }

    def action_chat_estimate(self):
        """Chat-style: paste work order text → extract stops → calculate full estimate → show formatted result."""
        self.ensure_one()
        text = (self.x_chat_message or '').strip()
        if not text:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'No message', 'message': 'Paste your work order or text message first.', 'type': 'warning'},
            }
        if not self.vehicle_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'Select a Truck', 'message': 'Choose a truck first so the estimate can include your home location and ELD fuel data.', 'type': 'warning'},
            }
        try:
            from ..services.mapbox_service import MapboxService
            from ..services.pricing_engine import PricingEngine

            stops, notes = self._extract_stops_from_text(text)
            if not stops:
                self.x_chat_response = '<p class="text-danger">⚠️ Could not detect any stops from this text. Please check the message and try again.</p>'
                return {'type': 'ir.actions.client', 'tag': 'reload'}

            # Populate stop_ids
            self.stop_ids.filtered(lambda s: not s.is_system).unlink()
            seq = 10
            for s in stops:
                self.env['premafirm.estimator.stop'].create({
                    'estimator_id': self.id,
                    'sequence': seq,
                    'stop_type': s.get('type', 'delivery'),
                    'name': s.get('company_name', ''),
                    'address': s.get('address', ''),
                    'lat': float(s.get('lat') or 0),
                    'lng': float(s.get('lng') or 0),
                    'pallets': int(s.get('pallets') or 0),
                    'weight_lbs': float(s.get('weight_lbs') or 0),
                    'liftgate': bool(s.get('liftgate')),
                    'notes': s.get('stop_notes', ''),
                })
                seq += 10
            if notes and not self.notes:
                self.notes = notes
            self.add_system_stops()
            total_pallets = sum(int(s.get('pallets') or 0) for s in stops if s.get('type') == 'pickup')
            if total_pallets:
                self.load_pallets = total_pallets

            # Build route using all stop_ids (includes home origin + return)
            vehicle = self.vehicle_id
            all_stops = self.stop_ids.sorted('sequence')
            waypoints = [{'lat': s.lat, 'lng': s.lng} for s in all_stops if s.lat and s.lng]
            if len(waypoints) < 2:
                self.x_chat_response = '<p class="text-danger">⚠️ Could not geocode enough stops to calculate a route. Check the addresses.</p>'
                return {'type': 'ir.actions.client', 'tag': 'reload'}

            mbx = MapboxService(self.env)
            route = mbx.get_route_multi(
                waypoints,
                max_height_ft=vehicle.x_vehicle_height_ft or 0.0,
                gvwr_lbs=vehicle.x_gvwr_lbs or 0.0,
                allow_cross_border=self.allow_cross_border,
                avoid_tolls=self.avoid_tolls,
            )

            eng = PricingEngine(self.env)
            defaults = self.get_defaults_rpc(vehicle.id)
            costs = eng.calculate(
                vehicle.id,
                route['distance_km'],
                route['duration_hrs'],
                overrides={
                    'fuel_price_per_l':    defaults.get('fuel_price_per_l', 0),
                    'driver_rate_per_hr':  defaults.get('driver_rate_per_hr', 0),
                    'insurance_monthly':   defaults.get('insurance_monthly', 0),
                    'maintenance_monthly': defaults.get('maintenance_monthly', 0),
                    'margin_pct':          defaults.get('margin_pct', 0),
                },
                load_weight_lbs=self.load_weight_lbs or 0.0,
            )

            # Persist cost result
            self.write({
                'origin_address':      all_stops[0].address,
                'destination_address': all_stops[-1].address,
                'distance_km':         route['distance_km'],
                'duration_hrs':        route['duration_hrs'],
                'fuel_liters':         costs['fuel_liters'],
                'fuel_load_factor':    costs['fuel_load_factor'],
                'fuel_cost':           costs['fuel_cost'],
                'maintenance_cost':    costs['maintenance_cost'],
                'insurance_cost':      costs['insurance_cost'],
                'driver_cost':         costs['driver_cost'],
                'weight_surcharge':    costs['weight_surcharge'],
                'total_cost':          costs['total_cost'],
                'margin_pct':          costs['margin_pct'],
                'suggested_rate':      costs['suggested_rate'],
                'fuel_price_per_l':    costs['fuel_price_per_l'],
                'driver_rate_per_hr':  costs['driver_rate_per_hr'],
                'avg_km_per_l':        costs['avg_km_per_l'],
                'state':               'saved',
            })

            # Build source labels for transparency
            avg_kml_src = 'GeoTab ELD (last 7 days)' if vehicle.x_avg_km_per_l_last_week else 'Manual entry'
            prev_km_src = 'GeoTab Daily Odometer' if costs['prev_month_km'] != (vehicle.x_monthly_avg_km or 0) else 'Manual (Monthly Avg km field)'

            # Build formatted response
            legs = route.get('legs', [])
            stop_rows = ''
            for s in all_stops:
                icon = '🏠' if s.stop_type in ('origin', 'return') else ('📦' if s.stop_type == 'pickup' else '📍')
                pallet_txt = f' — {s.pallets} plt' if s.pallets else ''
                stop_rows += f'<tr><td>{icon} {s.stop_type.title()}</td><td>{s.name or ""}</td><td>{s.address}</td><td>{pallet_txt}</td></tr>'

            leg_rows = ''
            for i, leg in enumerate(legs):
                leg_rows += f'<tr><td>Leg {i+1}</td><td>{leg.get("distance_km", 0):.1f} km</td><td>{leg.get("duration_hrs", 0):.1f} hrs</td></tr>'

            html = f'''
<div style="font-family:sans-serif;font-size:13px;line-height:1.6">
  <h4 style="margin-bottom:8px">🚛 Estimate: {vehicle.name}</h4>

  <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
    <thead><tr style="background:#f0f0f0"><th style="text-align:left;padding:4px">Stop</th><th>Company</th><th>Address</th><th>Pallets</th></tr></thead>
    <tbody>{stop_rows}</tbody>
  </table>

  <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
    <thead><tr style="background:#f0f0f0"><th style="text-align:left;padding:4px">Route</th><th>Distance</th><th>Drive Time</th></tr></thead>
    <tbody>
      {leg_rows}
      <tr style="font-weight:bold"><td>Total</td><td>{route["distance_km"]:.1f} km</td><td>{route["duration_hrs"]:.1f} hrs</td></tr>
    </tbody>
  </table>

  <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
    <thead><tr style="background:#f0f0f0"><th style="text-align:left;padding:4px">Cost</th><th>Amount</th><th>Source</th></tr></thead>
    <tbody>
      <tr><td>⛽ Fuel ({costs["fuel_liters"]:.1f} L × ${costs["fuel_price_per_l"]:.3f}/L)</td><td>${costs["fuel_cost"]:.2f}</td><td>{avg_kml_src} — {costs["avg_km_per_l"]:.2f} km/L</td></tr>
      <tr><td>🔧 Maintenance</td><td>${costs["maintenance_cost"]:.2f}</td><td>Monthly budget ÷ {prev_km_src} km</td></tr>
      <tr><td>🛡️ Insurance</td><td>${costs["insurance_cost"]:.2f}</td><td>Monthly budget ÷ {prev_km_src} km</td></tr>
      <tr><td>👤 Driver ({route["duration_hrs"]:.1f} hrs × ${costs["driver_rate_per_hr"]:.2f}/hr)</td><td>${costs["driver_cost"]:.2f}</td><td>Truck prefs / system default</td></tr>
      {"<tr><td>⚖️ Weight Surcharge</td><td>$" + f"{costs['weight_surcharge']:.2f}" + "</td><td>Load over threshold</td></tr>" if costs["weight_surcharge"] else ""}
      <tr style="font-weight:bold;background:#fff3cd"><td>Total Cost</td><td>${costs["total_cost"]:.2f}</td><td></td></tr>
      <tr style="font-weight:bold;background:#d4edda"><td>Suggested Rate ({costs["margin_pct"]:.0f}% margin)</td><td>${costs["suggested_rate"]:.2f}</td><td></td></tr>
    </tbody>
  </table>

  <p style="color:#888;font-size:11px">
    📡 Data sources: Fuel efficiency {costs["avg_km_per_l"]:.2f} km/L ({avg_kml_src}) ·
    Prev month km: {costs["prev_month_km"]:.0f} km ({prev_km_src}) ·
    Fuel price: ${costs["fuel_price_per_l"]:.3f}/L (truck prefs / system param)
  </p>
</div>'''

            self.x_chat_response = html
            return {'type': 'ir.actions.client', 'tag': 'reload'}

        except Exception as exc:
            _logger.error("action_chat_estimate error: %s", exc, exc_info=True)
            self.x_chat_response = f'<p class="text-danger">⚠️ Error: {exc}</p>'
            return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _extract_stops_from_text(self, text):
        """Use AI to extract all stops from a freeform text message.

        Returns (stops_list, notes_string).
        Each stop: {type, company_name, address, pallets, weight_lbs, liftgate, stop_notes, lat, lng}
        """
        import json as _json
        system_prompt = (
            'You are a Canadian freight dispatch assistant. Extract ALL pickup and delivery stops '
            'from the work order text. Return ONLY a JSON object:\n'
            '{"stops": [{"type": "pickup|delivery", "company_name": "...", '
            '"address": "full street address, city, province, Canada", '
            '"pallets": 0, "weight_lbs": 0, "liftgate": false, "stop_notes": "..."}], '
            '"notes": "any general trip notes"}\n'
            'Rules:\n'
            '- type must be "pickup" or "delivery"\n'
            '- address must be a full geocodable address (include city and province)\n'
            '- If only a business name is given with no address, use the name as address\n'
            '- pallets: integer count if mentioned, else 0\n'
            '- Keep stops in the order they appear in the text\n'
            '- Return ONLY the JSON, no explanation'
        )
        result_text = self._call_openai(
            system=system_prompt,
            user=f'Work order:\n\n{text[:3000]}',
            max_tokens=1200,
        )
        # Robust JSON extraction
        clean = result_text.strip()
        parsed = None
        for attempt in [clean,
                        re.sub(r'^```(?:json)?\s*', '', clean, flags=re.IGNORECASE).rstrip('`').strip()]:
            try:
                parsed = _json.loads(attempt)
                break
            except Exception:
                pass
        if parsed is None:
            start = clean.find('{')
            if start != -1:
                depth = 0
                in_str = esc = False
                for ci, ch in enumerate(clean[start:], start):
                    if esc:
                        esc = False
                        continue
                    if ch == '\\' and in_str:
                        esc = True
                        continue
                    if ch == '"':
                        in_str = not in_str
                        continue
                    if in_str:
                        continue
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                parsed = _json.loads(clean[start:ci + 1])
                            except Exception:
                                pass
                            break
        if not parsed:
            raise ValueError(f'AI returned unparseable response: {result_text[:200]}')

        stops = parsed.get('stops') or []
        notes = parsed.get('notes') or ''

        # Geocode each stop
        for stop in stops:
            addr = (stop.get('address') or '').strip()
            if addr and not (stop.get('lat') and stop.get('lng')):
                try:
                    hits = self.geocode_address_rpc(addr)
                    if hits:
                        stop['address'] = hits[0]['place_name']
                        stop['lat'] = hits[0]['lat']
                        stop['lng'] = hits[0]['lng']
                except Exception:
                    pass
            stop.setdefault('lat', 0)
            stop.setdefault('lng', 0)

        return stops, notes

    def _call_openai(self, system, user, max_tokens=600):
        """AI call via OpenAI."""
        from ..services.openai_utils import openai_chat, DEFAULT_MODEL
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("openai.api_key") or ICP.get_param("prema_ai.api_key")
        if not api_key:
            raise ValueError("OpenAI API key not configured (openai.api_key).")
        model = ICP.get_param("prema_ai.fast_model") or DEFAULT_MODEL
        return openai_chat(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=max_tokens,
            api_key=api_key,
            model=model,
            timeout=45,
        )

    @api.model
    def ai_rewrite_notes_rpc(self, estimator_id):
        """Return AI-rewritten notes without saving. Frontend uses response to show diff."""
        rec = self.sudo().browse(estimator_id)
        if not rec.exists():
            return {"error": "Estimate not found."}
        try:
            stops_summary = "; ".join(
                f"{s.stop_type} @ {s.address or 'unknown'}"
                for s in rec.stop_ids.sorted("sequence")
            ) if rec.stop_ids else (rec.stops_json or "")
            context = (
                f"Truck: {rec.vehicle_id.name if rec.vehicle_id else 'Unknown'}\n"
                f"Date: {rec.scheduled_at.strftime('%A %B %d %Y') if rec.scheduled_at else 'TBD'}\n"
                f"Route: {rec.origin_address or ''} → {rec.destination_address or ''}\n"
                f"Distance: {rec.distance_km:.0f} km\n"
                f"Stops: {stops_summary}\n"
                f"Pallets: {rec.load_pallets or 0}  Weight: {rec.load_weight_lbs:.0f} lbs\n"
                f"Current notes: {rec.notes or '(none)'}"
            )
            suggestion = self._call_openai(
                system="You are a professional logistics dispatcher. Rewrite the trip notes in clear, professional dispatch language. Return only the rewritten notes, no explanation.",
                user=context,
                max_tokens=400,
            )
            return {"success": True, "suggestion": suggestion, "original": rec.notes or ""}
        except Exception as e:
            _logger.error("ai_rewrite_notes_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    @api.model
    def ai_rewrite_notes_accept_rpc(self, estimator_id, new_notes):
        """Accept AI notes rewrite — saves to record."""
        rec = self.sudo().browse(estimator_id)
        if not rec.exists():
            return {"error": "Estimate not found."}
        rec.write({"notes": new_notes})
        return {"success": True}

    @api.model
    def ai_review_stops_rpc(self, estimator_id):
        """Return AI stop review suggestions without saving."""
        rec = self.sudo().browse(estimator_id)
        if not rec.exists():
            return {"error": "Estimate not found."}
        try:
            from ..services.invoice_ai_service import InvoiceAIService
            stops_list = [s.as_dict() for s in rec.stop_ids.sorted("sequence")] if rec.stop_ids else []
            if not stops_list:
                try:
                    stops_list = json.loads(rec.stops_json or "[]")
                except Exception:
                    stops_list = []
            prompt = (
                f"Review these delivery stops for a logistics trip on {rec.scheduled_at.strftime('%A %B %d %Y') if rec.scheduled_at else 'unknown date'}.\n"
                f"Truck: {rec.vehicle_id.name if rec.vehicle_id else 'Unknown'}\n\n"
                f"Current stops:\n{json.dumps(stops_list, indent=2)}\n\n"
                "Suggest any improvements: add missing stops, remove duplicates, reorder for efficiency, fix addresses.\n"
                "Return ONLY a JSON object with keys: suggestions (list of {action, stop_index, reason, new_stop?}), before (original list), after (improved list)."
            )
            raw = self._call_openai(system="You are a logistics route planner.", user=prompt, max_tokens=800)
            # Try to parse as JSON
            try:
                import re
                json_match = re.search(r"\{.*\}", raw, re.DOTALL)
                result = json.loads(json_match.group(0)) if json_match else {"suggestions": [], "before": stops_list, "after": stops_list}
            except Exception:
                result = {"suggestions": [], "before": stops_list, "after": stops_list, "raw": raw}
            result["success"] = True
            return result
        except Exception as e:
            _logger.error("ai_review_stops_rpc error: %s", e, exc_info=True)
            return {"error": str(e)}

    @api.model
    def ai_review_stops_apply_rpc(self, estimator_id, new_stops):
        """Apply accepted stop changes from AI review."""
        rec = self.sudo().browse(estimator_id)
        if not rec.exists():
            return {"error": "Estimate not found."}
        rec._json_to_stops(new_stops)
        rec._stops_to_json()
        return {"success": True, "stop_count": len(rec.stop_ids)}

    # ── Chat-style estimator panel RPC ─────────────────────────────────

    @api.model
    def chat_rate_request_rpc(
        self,
        vehicle_id,
        text_message,
        files,
        avoid_tolls=True,
        allow_cross_border=False,
        scheduled_at=None,
    ):
        """Unified RPC for the chat-style Estimator panel.

        text_message : free-form text (WhatsApp, email, pasted message)
        files        : list of {file_b64, mimetype, filename} — zero or many BOLs
        Returns      : {html, record_id, suggested_rate} or {error}
        """
        from ..services.mapbox_service import MapboxService
        from ..services.pricing_engine import PricingEngine

        try:
            vehicle = self.env["fleet.vehicle"].sudo().browse(int(vehicle_id))
            if not vehicle.exists():
                return {"error": f"Truck ID {vehicle_id} not found."}

            all_stops = []
            all_notes = []

            # 1. Extract stops from free-form text
            if text_message and text_message.strip():
                t_stops, t_notes = self._extract_stops_from_text(text_message)
                all_stops.extend(t_stops)
                if t_notes:
                    all_notes.append(t_notes)

            # 2. Extract stops from each uploaded file
            for f in (files or []):
                f_result = self.extract_stops_from_file_rpc(
                    f["file_b64"], f["mimetype"], f["filename"], extra_notes=""
                )
                if f_result.get("error"):
                    _logger.warning(
                        "chat_rate_request_rpc: file parse error for %s: %s",
                        f.get("filename"), f_result["error"],
                    )
                    continue
                f_stops = f_result.get("stops") or []
                for s in f_stops:
                    s["type"] = s.get("type") or "delivery"
                all_stops.extend(f_stops)
                if f_result.get("notes"):
                    all_notes.append(f_result["notes"])

            if not all_stops:
                return {
                    "error": (
                        "Could not detect any stops from the provided input. "
                        "Please check your message or file content and try again."
                    )
                }

            # 3. Create estimator record
            rec = self.sudo().create({
                "vehicle_id":        vehicle.id,
                "avoid_tolls":       avoid_tolls,
                "allow_cross_border": allow_cross_border,
                "notes":             "\n".join(all_notes) if all_notes else "",
            })

            # 4. Persist stops
            seq = 10
            for s in all_stops:
                self.env["premafirm.estimator.stop"].sudo().create({
                    "estimator_id": rec.id,
                    "sequence":     seq,
                    "stop_type":    s.get("type", "delivery"),
                    "name":         s.get("company_name", ""),
                    "address":      s.get("address", ""),
                    "lat":          float(s.get("lat") or 0),
                    "lng":          float(s.get("lng") or 0),
                    "pallets":      int(s.get("pallets") or 0),
                    "weight_lbs":   float(s.get("weight_lbs") or 0),
                    "liftgate":     bool(s.get("liftgate")),
                    "notes":        s.get("stop_notes", ""),
                })
                seq += 10

            # 5. Add origin / return home stops from truck GPS
            rec.add_system_stops()

            # 6. Set load totals from pickup stops
            total_pallets = sum(
                int(s.get("pallets") or 0)
                for s in all_stops
                if (s.get("type") or "").lower() in ("pickup",)
            )
            if total_pallets:
                rec.load_pallets = total_pallets

            # 7. Build multi-stop route
            ordered = rec.stop_ids.sorted("sequence")
            waypoints = [
                {"lat": s.lat, "lng": s.lng}
                for s in ordered
                if s.lat and s.lng
            ]
            if len(waypoints) < 2:
                return {
                    "error": (
                        "Could not geocode enough stops to calculate a route. "
                        "Please verify the addresses."
                    ),
                    "record_id": rec.id,
                }

            mbx   = MapboxService(self.env)
            route = mbx.get_route_multi(
                waypoints,
                max_height_ft=vehicle.x_vehicle_height_ft or 0.0,
                gvwr_lbs=vehicle.x_gvwr_lbs or 0.0,
                allow_cross_border=allow_cross_border,
                avoid_tolls=avoid_tolls,
            )

            # 8. Calculate costs
            defaults = rec.get_defaults_rpc(vehicle.id)
            eng      = PricingEngine(self.env)
            costs    = eng.calculate(
                vehicle.id,
                route["distance_km"],
                route["duration_hrs"],
                overrides={
                    "fuel_price_per_l":    defaults.get("fuel_price_per_l", 0),
                    "driver_rate_per_hr":  defaults.get("driver_rate_per_hr", 0),
                    "insurance_monthly":   defaults.get("insurance_monthly", 0),
                    "maintenance_monthly": defaults.get("maintenance_monthly", 0),
                    "margin_pct":          defaults.get("margin_pct", 0),
                },
                load_weight_lbs=rec.load_weight_lbs,
            )

            # 9. Persist computed values on the record
            rec.write({
                "origin_address":      ordered[0].address,
                "destination_address": ordered[-1].address,
                "distance_km":         route["distance_km"],
                "duration_hrs":        route["duration_hrs"],
                "fuel_liters":         costs["fuel_liters"],
                "fuel_load_factor":    costs["fuel_load_factor"],
                "fuel_cost":           costs["fuel_cost"],
                "maintenance_cost":    costs["maintenance_cost"],
                "insurance_cost":      costs["insurance_cost"],
                "driver_cost":         costs["driver_cost"],
                "weight_surcharge":    costs["weight_surcharge"],
                "total_cost":          costs["total_cost"],
                "margin_pct":          costs["margin_pct"],
                "suggested_rate":      costs["suggested_rate"],
                "fuel_price_per_l":    costs["fuel_price_per_l"],
                "driver_rate_per_hr":  costs["driver_rate_per_hr"],
                "state":               "saved",
            })

            # 10. Build formatted HTML summary
            html = self._build_chat_estimate_html(vehicle, ordered, route, costs)

            return {
                "html":           html,
                "record_id":      rec.id,
                "suggested_rate": costs["suggested_rate"],
            }

        except Exception as exc:
            _logger.error("chat_rate_request_rpc error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    def _build_chat_estimate_html(self, vehicle, ordered_stops, route, costs):
        """Return an HTML string summarising the estimate for display in the panel."""
        avg_kml_src = (
            "GeoTab ELD (last 7 days)"
            if vehicle.x_avg_km_per_l_last_week
            else "Manual entry"
        )

        stop_rows = ""
        for s in ordered_stops:
            icon = (
                "🏠" if s.stop_type in ("origin", "return")
                else ("📦" if s.stop_type == "pickup" else "📍")
            )
            pallet_txt = f"{s.pallets} plt" if s.pallets else "—"
            stop_rows += (
                f"<tr>"
                f"<td style='padding:3px 6px'>{icon} {s.stop_type.title()}</td>"
                f"<td style='padding:3px 6px'>{s.name or '—'}</td>"
                f"<td style='padding:3px 6px'>{s.address}</td>"
                f"<td style='padding:3px 6px;text-align:center'>{pallet_txt}</td>"
                f"</tr>"
            )

        legs      = route.get("legs", [])
        leg_rows  = ""
        for i, leg in enumerate(legs):
            leg_rows += (
                f"<tr>"
                f"<td style='padding:3px 6px'>Leg {i + 1}</td>"
                f"<td style='padding:3px 6px'>{leg.get('distance_km', 0):.1f} km</td>"
                f"<td style='padding:3px 6px'>{leg.get('duration_hrs', 0):.1f} hrs</td>"
                f"</tr>"
            )

        weight_row = ""
        if costs["weight_surcharge"]:
            weight_row = (
                f"<tr><td style='padding:3px 6px'>⚖️ Weight Surcharge</td>"
                f"<td style='padding:3px 6px'>${costs['weight_surcharge']:.2f}</td></tr>"
            )

        th = "style='text-align:left;padding:4px 6px;background:#f5f5f5;font-weight:600'"

        html = f"""
<div style="font-family:system-ui,sans-serif;font-size:12.5px;line-height:1.55">
  <p style="margin:0 0 10px;font-weight:700;font-size:14px">
    🚛 {vehicle.name}
    <span style="float:right;color:#27ae60;font-size:16px">${costs['suggested_rate']:.2f}</span>
  </p>

  <table style="width:100%;border-collapse:collapse;margin-bottom:12px;font-size:12px">
    <thead>
      <tr>
        <th {th}>Stop</th><th {th}>Company</th>
        <th {th}>Address</th><th {th}>Plt</th>
      </tr>
    </thead>
    <tbody>{stop_rows}</tbody>
  </table>

  <table style="width:100%;border-collapse:collapse;margin-bottom:12px;font-size:12px">
    <thead>
      <tr><th {th}>Route</th><th {th}>Distance</th><th {th}>Drive</th></tr>
    </thead>
    <tbody>
      {leg_rows}
      <tr style="font-weight:bold">
        <td style='padding:3px 6px'>Total</td>
        <td style='padding:3px 6px'>{route['distance_km']:.1f} km</td>
        <td style='padding:3px 6px'>{route['duration_hrs']:.1f} hrs</td>
      </tr>
    </tbody>
  </table>

  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <tbody>
      <tr>
        <td style='padding:3px 6px'>⛽ Fuel ({costs['fuel_liters']:.1f} L × ${costs['fuel_price_per_l']:.3f}/L)</td>
        <td style='padding:3px 6px;text-align:right'>${costs['fuel_cost']:.2f}</td>
      </tr>
      <tr>
        <td style='padding:3px 6px'>🔧 Maintenance</td>
        <td style='padding:3px 6px;text-align:right'>${costs['maintenance_cost']:.2f}</td>
      </tr>
      <tr>
        <td style='padding:3px 6px'>🛡️ Insurance</td>
        <td style='padding:3px 6px;text-align:right'>${costs['insurance_cost']:.2f}</td>
      </tr>
      <tr>
        <td style='padding:3px 6px'>👤 Driver ({route['duration_hrs']:.1f} hrs × ${costs['driver_rate_per_hr']:.2f}/hr)</td>
        <td style='padding:3px 6px;text-align:right'>${costs['driver_cost']:.2f}</td>
      </tr>
      {weight_row}
      <tr style="font-weight:700;background:#fff3cd">
        <td style='padding:4px 6px'>Total Cost</td>
        <td style='padding:4px 6px;text-align:right'>${costs['total_cost']:.2f}</td>
      </tr>
      <tr style="font-weight:700;background:#d4edda">
        <td style='padding:4px 6px'>Suggested Rate ({costs['margin_pct']:.0f}% margin)</td>
        <td style='padding:4px 6px;text-align:right'>${costs['suggested_rate']:.2f}</td>
      </tr>
    </tbody>
  </table>

  <p style="color:#999;font-size:10.5px;margin-top:8px;margin-bottom:0">
    Fuel efficiency: {costs['avg_km_per_l']:.2f} km/L ({avg_kml_src}) ·
    Fuel price: ${costs['fuel_price_per_l']:.3f}/L
  </p>
</div>"""
        return html
