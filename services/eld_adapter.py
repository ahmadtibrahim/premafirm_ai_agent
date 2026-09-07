"""ELD / truck-status adapter boundary (master §11.2).

Prema AI and Prema Dispatch must never couple business logic to one ELD
provider.  This module is the single boundary that normalizes vehicle
position and driver duty status into provider-agnostic values; concrete
providers (Geotab today, others later) plug in behind the same surface.

Design rules:
  - Read-through first: cached vehicle/driver fields (x_last_location_*,
    x_last_duty_status, x_last_eld_sync_at) are the default source.  When
    the Geotab live service is enabled it stays the authority for fresher
    data, but the adapter never crashes the estimator when telematics is
    stale or absent — it reports ``fresh: False`` with a warning instead.
  - Honesty rule (§11.5): when a field class does not exist on the model
    (e.g. maintenance scheduling), the adapter returns ``known: False`` and
    a warning — it never pretends a truck is maintenance-clear.
  - Nothing here mutates state; this is a read-only normalization layer.

Field sources (engine-owned fleet.vehicle extensions and res.partner
driver fields; see models/fleet_vehicle_extension.py and
models/res_partner_extension.py).
"""

import datetime
import logging

_logger = logging.getLogger(__name__)

# Raw Geotab duty values → normalized duty. The engine stores the Geotab
# raw codes; the availability code compares normalized states.
DUTY_MAP = {
    "D": "driving",
    "ON": "on_duty",
    "OFF": "off_duty",
    "SB": "sleeper",
    "driving": "driving",
    "on_duty": "on_duty",
    "off_duty": "off_duty",
    "sleeper": "sleeper",
    "yd": "driving",
    "nyd": "on_duty",
}
NORMALIZED_DUTIES = ("driving", "on_duty", "off_duty", "sleeper", "unknown")


class EldAdapter:
    """Provider-agnostic vehicle/driver status boundary (read-only)."""

    # Providers that expose live duty/position behind the same surface.
    PROVIDERS = {"geotab", "manual"}

    def __init__(self, env):
        self.env = env
        self._geotab_service = None

    # ── Public surface ──────────────────────────────────────────────

    def vehicle_eld_status(self, vehicle):
        """Normalized status block for one fleet.vehicle, plus warnings.

        Returns a dict consumed by the scenario request pipeline (never
        raises; a vehicle without any ELD linkage reports provider False
        and an explanatory warning).
        """
        vehicle = vehicle.sudo()
        if not vehicle:
            return self._empty("no_vehicle")

        driver = False
        if vehicle.x_current_driver_contact_id:
            driver = vehicle.x_current_driver_contact_id
        elif vehicle.driver_id and vehicle.driver_id.partner_id:
            driver = vehicle.driver_id.partner_id

        # Duty source is the LAST GEOTAB LOGGED duty (res.partner
        # x_last_duty_status: D/ON/OFF/SB), NOT the HR profile selection
        # x_driver_status (active/inactive/on_leave) — the HR flag never
        # tells us what the driver is doing right now.  Unknown stays
        # unknown (honest warning below); never map the HR flag to a duty.
        duty_raw = ""
        if driver and "x_last_duty_status" in driver._fields:
            duty_raw = driver.x_last_duty_status or ""
        duty = DUTY_MAP.get(str(duty_raw).strip().upper(),
                            str(duty_raw) if str(duty_raw) in NORMALIZED_DUTIES else "")
        if not duty:
            duty = "unknown"

        position, pos_fresh = self._position(vehicle)
        sync = self._sync_state(vehicle)
        maintenance = self._maintenance_state(vehicle)

        warnings = list(sync["warnings"]) + list(maintenance["warnings"])
        if not driver:
            warnings.append(
                "No driver is assigned to this truck — driver hours and "
                "duty status are unknown.")
        if duty == "unknown":
            warnings.append(
                "Driver duty status is unknown — availability is "
                "best-effort until the next ELD sync.")

        provider = "geotab" if (
            vehicle.x_geotab_device_id or vehicle.x_sync_status
        ) else "manual"
        live = bool(provider == "geotab" and sync["fresh"])

        return {
            "provider": provider,
            "live": live,
            "duty": duty,
            "duty_raw": duty_raw or None,
            "driver_id": driver.id if driver else False,
            "driver_name": driver.name if driver else "",
            "driver_phone": driver.phone or driver.mobile or "" if driver else "",
            "position": position,
            "position_fresh": pos_fresh,
            "eld_sync_at": sync["eld_sync_at"],
            "telematics_updated_at": sync["telematics_at"],
            "fresh": live and pos_fresh,
            "maintenance_known": maintenance["known"],
            "warnings": warnings,
        }

    def duty_is_available(self, duty):
        """Normalized-duty availability: only an explicit OFF duty (or
        unknown data with a warning) leaves the truck free to start a new
        move. Driving/on-duty/sleeper counts as occupied."""
        return str(duty) == "off_duty"

    # ── Internals ───────────────────────────────────────────────────

    def _empty(self, reason):
        return {
            "provider": False, "live": False, "duty": "unknown",
            "duty_raw": None, "driver_id": False, "driver_name": "",
            "driver_phone": "", "position": False, "position_fresh": False,
            "eld_sync_at": False, "telematics_updated_at": False,
            "fresh": False, "maintenance_known": False,
            "warnings": ["No truck selected — ELD status unavailable (%s)."
                         % reason],
        }

    def _position(self, vehicle):
        """Latest known truck position from cached telematics fields."""
        lat = vehicle.x_last_location_lat
        lng = vehicle.x_last_location_lng
        if not lat or not lng:
            return False, False
        updated = vehicle.x_last_location_at
        fresh = bool(updated) and (
            datetime.datetime.utcnow() - self._as_naive_utc(updated)
        ).total_seconds() < 4 * 3600
        return {
            "lat": round(float(lat), 6),
            "lng": round(float(lng), 6),
            "address": vehicle.x_last_location_address or "",
            "updated_at": fields_datetime_iso(updated),
        }, fresh

    def _sync_state(self, vehicle):
        warnings = []
        eld_sync_at = vehicle.x_last_eld_sync_at
        telematics_at = vehicle.x_last_valid_telematics_at
        if vehicle.x_sync_status and vehicle.x_sync_error:
            warnings.append(
                "Last ELD sync failed (%s) — status may be stale."
                % (vehicle.x_sync_error or "error"))
        if eld_sync_at and (datetime.datetime.utcnow() -
                            self._as_naive_utc(eld_sync_at)).total_seconds() \
                > 24 * 3600:
            warnings.append(
                "ELD data is older than 24 h — driver duty status may be "
                "stale.")
        return {
            "eld_sync_at": fields_datetime_iso(eld_sync_at),
            "telematics_at": fields_datetime_iso(telematics_at),
            "fresh": bool(eld_sync_at) or bool(telematics_at),
            "warnings": warnings,
        }

    @staticmethod
    def _maintenance_state(vehicle):
        """No maintenance-due model exists anywhere on fleet.vehicle —
        report the gap honestly instead of asserting a clear truck."""
        if vehicle.x_maintenance_cost_per_km or vehicle.x_monthly_maintenance_budget:
            return {
                "known": True,
                "warnings": [
                    "Maintenance cost is budgeted, but no preventive-"
                    "maintenance due-date data exists for this truck."],
            }
        return {
            "known": False,
            "warnings": [
                "No maintenance data exists for this truck — maintenance "
                "status is unknown."],
        }

    @staticmethod
    def _as_naive_utc(value):
        if not value:
            return datetime.datetime(1970, 1, 1)
        if isinstance(value, datetime.datetime):
            dt = value
        else:
            try:
                dt = datetime.datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return datetime.datetime(1970, 1, 1)
        if dt.tzinfo:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt


def fields_datetime_iso(value):
    """Odoo naive-UTC Datetime/string → ISO string ('' when unset)."""
    if not value:
        return ""
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return datetime.datetime.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return ""
