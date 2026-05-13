import logging
from datetime import datetime, timedelta, timezone

import requests

_logger = logging.getLogger(__name__)

GEOTAB_DEFAULT_SERVER = "my.geotab.com"

# Well-known Geotab system diagnostic IDs
DIAG_ODOMETER = "DiagnosticOdometerId"
DIAG_ENGINE_HOURS = "DiagnosticEngineHoursId"

# Fuel level — try percentage form first (0.0–1.0), fall back to direct % form (0–100)
DIAG_FUEL_PERCENT = "DiagnosticFuelLevelPercentageId"   # returns 0.0–1.0 → ×100
DIAG_FUEL_LEVEL = "DiagnosticFuelLevelId"               # returns 0–100 directly

# DEF level — try standard form first, fall back to OBD direct form
DIAG_DEF_PERCENT = "DiagnosticDieselExhaustFluidLevelId"  # returns 0.0–1.0 → ×100
DIAG_DEF_LEVEL = "DiagnosticDieselExhaustFluidId"          # returns 0–100 directly

# J1939 cumulative fuel counter — monotonically increasing total lifetime fuel (liters)
# Delta between two readings = fuel consumed in that period.
DIAG_TOTAL_FUEL_USED = "DiagnosticEngineTotalFuelUsedId"


class GeotabService:
    def __init__(self, env):
        self.env = env
        self._credentials = None
        self._base_url = None

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _get_params(self):
        p = self.env["ir.config_parameter"].sudo()
        return {
            "enabled": p.get_param("geotab.enabled", "False").lower() == "true",
            "database": p.get_param("geotab.database", ""),
            "username": p.get_param("geotab.username", ""),
            "password": p.get_param("geotab.password", ""),
            "server_url": p.get_param("geotab.server_url", GEOTAB_DEFAULT_SERVER),
        }

    def _api_url(self, server=None):
        s = server or self._base_url or GEOTAB_DEFAULT_SERVER
        if not s.startswith("http"):
            s = f"https://{s}"
        if not s.endswith("/apiv1"):
            s = s.rstrip("/") + "/apiv1"
        return s

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self):
        cfg = self._get_params()
        if not cfg["enabled"]:
            raise ValueError("Geotab integration is disabled in system parameters.")
        url = self._api_url(cfg["server_url"])
        payload = {
            "method": "Authenticate",
            "params": {
                "database": cfg["database"],
                "userName": cfg["username"],
                "password": cfg["password"],
            },
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(f"Geotab authentication failed: {data['error']}")
        result = data.get("result", {})
        self._credentials = result.get("credentials", {})
        redirect = result.get("path", "")
        if redirect and redirect.lower() != "thisserver":
            self._base_url = f"https://{redirect}"
        else:
            self._base_url = self._api_url(cfg["server_url"]).replace("/apiv1", "")
        return self._credentials

    # ------------------------------------------------------------------
    # Core API call with automatic re-auth on session expiry
    # ------------------------------------------------------------------

    def call(self, method, type_name=None, search=None, entity=None, result_limit=None):
        if not self._credentials:
            self.authenticate()
        return self._execute(method, type_name, search, entity, result_limit)

    def _execute(self, method, type_name=None, search=None, entity=None, result_limit=None, _retry=True):
        params = {"credentials": self._credentials}
        if type_name:
            params["typeName"] = type_name
        if search is not None:
            params["search"] = search
        if entity is not None:
            params["entity"] = entity
        if result_limit is not None:
            params["resultsLimit"] = result_limit
        payload = {"method": method, "params": params}
        url = self._api_url()
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(f"Geotab request failed: {exc}") from exc
        data = resp.json()
        if "error" in data:
            err = str(data["error"])
            if _retry and "InvalidUserException" in err:
                self._credentials = None
                self.authenticate()
                return self._execute(method, type_name, search, entity, result_limit, _retry=False)
            raise ValueError(f"Geotab API error [{method}]: {err}")
        return data.get("result") or []

    # ------------------------------------------------------------------
    # Test connection
    # ------------------------------------------------------------------

    def test_connection(self):
        try:
            creds = self.authenticate()
            db = creds.get("database", "")
            user = creds.get("userName", "")
            return {"success": True, "message": f"Connected to database '{db}' as '{user}'."}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # ------------------------------------------------------------------
    # Devices / Vehicles
    # ------------------------------------------------------------------

    def get_devices(self):
        return self.call("Get", type_name="Device") or []

    def get_device_status_info(self, device_id=None):
        search = {}
        if device_id:
            search["deviceSearch"] = {"id": device_id}
        return self.call("Get", type_name="DeviceStatusInfo", search=search) or []

    def get_status_data(self, device_id, diagnostic_id, from_date=None, to_date=None):
        search = {
            "deviceSearch": {"id": device_id},
            "diagnosticSearch": {"id": diagnostic_id},
        }
        if from_date:
            search["fromDate"] = _fmt_dt(from_date)
        if to_date:
            search["toDate"] = _fmt_dt(to_date)
        results = self.call("Get", type_name="StatusData", search=search, result_limit=1) or []
        return results[0] if results else None

    def get_status_data_all(self, device_id, diagnostic_id, from_date=None, to_date=None):
        """Return ALL StatusData records for a device/diagnostic over the date range."""
        search = {
            "deviceSearch": {"id": device_id},
            "diagnosticSearch": {"id": diagnostic_id},
        }
        if from_date:
            search["fromDate"] = _fmt_dt(from_date)
        if to_date:
            search["toDate"] = _fmt_dt(to_date)
        return self.call("Get", type_name="StatusData", search=search) or []

    def get_trips(self, device_id=None, from_date=None, to_date=None, driver_id=None):
        search = {}
        if device_id:
            search["deviceSearch"] = {"id": device_id}
        if driver_id:
            search["driverSearch"] = {"id": driver_id}
        if from_date:
            search["fromDate"] = _fmt_dt(from_date)
        if to_date:
            search["toDate"] = _fmt_dt(to_date)
        return self.call("Get", type_name="Trip", search=search) or []

    # ------------------------------------------------------------------
    # Fuel
    # ------------------------------------------------------------------

    def get_fuel_transactions(self, device_id=None, from_date=None, to_date=None):
        date_search = {}
        if from_date:
            date_search["fromDate"] = _fmt_dt(from_date)
        if to_date:
            date_search["toDate"] = _fmt_dt(to_date)

        # Primary: filter by deviceSearch (not always supported)
        if device_id:
            try:
                result = self.call("Get", type_name="FuelTransaction",
                                   search={"deviceSearch": {"id": device_id}, **date_search}) or []
                if result:
                    return result
            except Exception:
                pass

        # Fallback: fetch all for the date range, filter by device in Python
        try:
            all_txns = self.call("Get", type_name="FuelTransaction", search=date_search) or []
            if device_id:
                all_txns = [t for t in all_txns
                            if (t.get("device") or {}).get("id") == device_id]
            if all_txns:
                _logger.debug("FuelTransaction: deviceSearch empty, found %d via full-scan filter", len(all_txns))
            return all_txns
        except Exception as exc:
            _logger.warning("FuelTransaction query failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Drivers
    # ------------------------------------------------------------------

    def get_drivers(self):
        return self.call("Get", type_name="User", search={"isDriver": True}) or []

    def get_duty_status_logs(self, driver_id=None, from_date=None, to_date=None):
        search = {}
        if driver_id:
            search["userSearch"] = {"id": driver_id}
        if from_date:
            search["fromDate"] = _fmt_dt(from_date)
        if to_date:
            search["toDate"] = _fmt_dt(to_date)
        return self.call("Get", type_name="DutyStatusLog", search=search) or []

    # ------------------------------------------------------------------
    # Helpers to extract telematics from raw responses
    # ------------------------------------------------------------------

    @staticmethod
    def extract_odometer_km(status_data_record):
        if not status_data_record:
            return None
        val = status_data_record.get("data")
        if val is None:
            return None
        return float(val) / 1000.0  # Geotab returns meters

    @staticmethod
    def extract_engine_hours(status_data_record):
        if not status_data_record:
            return None
        val = status_data_record.get("data")
        if val is None:
            return None
        return float(val) / 3600.0  # Geotab returns seconds

    @staticmethod
    def extract_fuel_percent(status_data_record):
        """For diagnostics that return a 0.0–1.0 fraction (e.g. DiagnosticFuelLevelPercentageId)."""
        if not status_data_record:
            return None
        val = status_data_record.get("data")
        if val is None:
            return None
        return float(val) * 100.0

    @staticmethod
    def extract_fuel_level_direct(status_data_record):
        """For diagnostics that return a value already in 0–100 % (e.g. DiagnosticFuelLevelId)."""
        if not status_data_record:
            return None
        val = status_data_record.get("data")
        if val is None:
            return None
        return float(val)


    @staticmethod
    def extract_location(device_status_info_record):
        if not device_status_info_record:
            return None, None, None
        lat = device_status_info_record.get("latitude")
        lng = device_status_info_record.get("longitude")
        dt_str = device_status_info_record.get("dateTime")
        dt = _parse_geotab_dt(dt_str) if dt_str else None
        return lat, lng, dt

    @staticmethod
    def device_matches_vehicle(device, vehicle):
        """Return True if a Geotab device record matches an Odoo fleet.vehicle."""
        d_vin = (device.get("vehicleIdentificationNumber") or "").strip().upper()
        d_plate = (device.get("licensePlate") or "").strip().upper()
        d_name = (device.get("name") or "").strip().upper()
        d_id = (device.get("id") or "").strip()

        # Already linked
        if vehicle.x_geotab_device_id and vehicle.x_geotab_device_id == d_id:
            return True

        v_vin = (vehicle.vin_sn or "").strip().upper()
        v_plate = (vehicle.license_plate or "").strip().upper()
        v_name = (vehicle.name or "").strip().upper()

        if d_vin and v_vin and d_vin == v_vin:
            return True
        if d_plate and v_plate and d_plate == v_plate:
            return True
        if d_name and v_name and d_name == v_name:
            return True
        return False

    @staticmethod
    def driver_matches_contact(gt_driver, contact):
        """Return True if a Geotab driver record matches an Odoo res.partner contact."""
        d_email = (gt_driver.get("email") or "").strip().lower()
        d_first = (gt_driver.get("firstName") or "").strip()
        d_last = (gt_driver.get("lastName") or "").strip()
        d_full = f"{d_first} {d_last}".strip()
        d_id = (gt_driver.get("id") or "").strip()

        if contact.x_geotab_driver_id and contact.x_geotab_driver_id == d_id:
            return True

        c_email = (contact.email or "").strip().lower()
        c_name = (contact.name or "").strip()

        if d_email and c_email and d_email == c_email:
            return True
        if d_full and c_name and d_full.lower() == c_name.lower():
            return True
        return False


# ------------------------------------------------------------------
# Module-level date helpers
# ------------------------------------------------------------------

def _fmt_dt(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return str(dt)


def _normalize_fuel_pct(raw):
    """Convert a raw Geotab fuel/DEF value to a 0–100 percentage, or None.

    Geotab devices are inconsistent: some return a 0.0–1.0 fraction
    (DiagnosticFuelLevelPercentageId), others return 0–100 directly.
    Distinguish by magnitude: any value > 1.0 is already on the 0–100 scale.
    """
    if raw is None:
        return None
    f = float(raw)
    if f <= 0:
        return None
    pct = f if f > 1.0 else f * 100.0
    return pct if 0 < pct <= 100 else None


def fetch_fuel_percent(svc, device_id):
    """Return fuel level % (0–100).
    Tries DiagnosticFuelLevelId first (direct 0–100, standard for J1939/CAN trucks);
    falls back to DiagnosticFuelLevelPercentageId (0.0–1.0 fraction, OBD-II)."""
    rec = svc.get_status_data(device_id, DIAG_FUEL_LEVEL)
    if rec is not None:
        pct = _normalize_fuel_pct(rec.get("data"))
        if pct is not None:
            return pct
    rec = svc.get_status_data(device_id, DIAG_FUEL_PERCENT)
    if rec is not None:
        pct = _normalize_fuel_pct(rec.get("data"))
        if pct is not None:
            return pct
    return None


def fetch_def_percent(svc, device_id):
    """Return DEF level % (0–100).
    Tries DiagnosticDieselExhaustFluidLevelId first; falls back to DiagnosticDieselExhaustFluidId."""
    rec = svc.get_status_data(device_id, DIAG_DEF_PERCENT)
    if rec is not None:
        pct = _normalize_fuel_pct(rec.get("data"))
        if pct is not None:
            return pct
    rec = svc.get_status_data(device_id, DIAG_DEF_LEVEL)
    if rec is not None:
        pct = _normalize_fuel_pct(rec.get("data"))
        if pct is not None:
            return pct
    return None


def _parse_geotab_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
