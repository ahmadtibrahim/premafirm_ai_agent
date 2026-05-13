"""
Fleetbase API service — create orders and check fleet availability.
"""
import json
import logging
import math
from datetime import datetime, timedelta
import urllib.parse
import urllib.error
import urllib.request

_logger = logging.getLogger(__name__)


class FleetbaseService:
    def __init__(self, env):
        self.env = env
        self._cfg_cache = None

    def _cfg(self):
        if not self._cfg_cache:
            ICP = self.env["ir.config_parameter"].sudo()
            self._cfg_cache = {
                "api_url": (ICP.get_param("fleetbase.api_url") or "").rstrip("/"),
                "api_key": ICP.get_param("fleetbase.api_key") or "",
            }
        return self._cfg_cache

    def _request(self, method, path, payload=None):
        cfg = self._cfg()
        if not cfg["api_key"]:
            raise ValueError(
                "Fleetbase API key not configured. "
                "Set it in Settings › Logistics › Fleetbase."
            )
        if not cfg["api_url"]:
            raise ValueError(
                "Fleetbase API URL not configured. "
                "Set it in Settings › Logistics › Fleetbase."
            )
        url = f"{cfg['api_url']}/{path.lstrip('/')}"
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Odoo/18-PremaFirm",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            _logger.error("Fleetbase %s %s → %s: %s", method, path, e.code, body)
            raise ValueError(f"Fleetbase API error {e.code}: {body}") from e

    def _with_query(self, path, params=None):
        params = {k: v for k, v in (params or {}).items() if v not in (None, "", False)}
        if not params:
            return path
        return f"{path}?{urllib.parse.urlencode(params)}"

    def _coerce_collection(self, result):
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("data", "results", "items", "records", "orders"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
            return [result]
        return []

    def _parse_dt(self, value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        text = str(value).strip()
        if not text:
            return None
        candidates = [
            text,
            text.replace("Z", "+00:00"),
        ]
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

    def _extract_stop_datetimes(self, stop):
        stop = stop or {}
        date_val = stop.get("date") or stop.get("stop_date")
        start_val = stop.get("time_window_start") or stop.get("arrival_time") or stop.get("scheduled_at")
        end_val = stop.get("time_window_end") or stop.get("departure_time")

        if date_val and start_val and "T" not in str(start_val):
            start = self._parse_dt(f"{date_val} {start_val}")
        else:
            start = self._parse_dt(start_val) or self._parse_dt(date_val)

        if date_val and end_val and "T" not in str(end_val):
            end = self._parse_dt(f"{date_val} {end_val}")
        else:
            end = self._parse_dt(end_val)

        return start, end

    def _normalize_schedule_item(self, item, fallback_duration_hrs=8.0):
        item = item or {}
        pickup = item.get("pickup") or {}
        dropoffs = item.get("dropoffs") or item.get("stops") or []
        first_drop = dropoffs[0] if isinstance(dropoffs, list) and dropoffs else {}

        start = (
            self._parse_dt(item.get("scheduled_at"))
            or self._parse_dt(item.get("scheduled_start"))
            or self._parse_dt(item.get("start_time"))
            or self._parse_dt(item.get("starts_at"))
        )
        end = (
            self._parse_dt(item.get("scheduled_end"))
            or self._parse_dt(item.get("end_time"))
            or self._parse_dt(item.get("ends_at"))
            or self._parse_dt(item.get("completed_at"))
        )

        pickup_start, pickup_end = self._extract_stop_datetimes(pickup)
        drop_start, drop_end = self._extract_stop_datetimes(first_drop)
        start = start or pickup_start or drop_start
        end = end or drop_end or pickup_end
        if start and not end:
            end = start + timedelta(hours=fallback_duration_hrs)

        vehicle = item.get("vehicle") or {}
        driver = item.get("driver") or {}
        driver_vehicle = (driver.get("vehicle") or {}) if isinstance(driver, dict) else {}
        meta = item.get("meta") or {}

        vehicle_id = (
            item.get("vehicle_id")
            or vehicle.get("id")
            or driver_vehicle.get("id")
            or meta.get("preferred_vehicle_id")
        )
        vehicle_name = (
            item.get("vehicle_name")
            or vehicle.get("name")
            or driver_vehicle.get("name")
            or meta.get("preferred_vehicle")
            or meta.get("assigned_vehicle")
            or ""
        )
        tracking = item.get("tracking_number") or item.get("public_id") or item.get("id") or ""
        return {
            "id": item.get("id") or item.get("public_id") or "",
            "tracking_number": tracking,
            "status": item.get("status") or "",
            "vehicle_id": str(vehicle_id or ""),
            "vehicle_name": str(vehicle_name or ""),
            "start": start,
            "end": end,
            "label": item.get("name") or tracking or item.get("status") or "Scheduled job",
            "source": item.get("source") or "order",
        }

    def test_connection(self):
        """Ping Fleetbase API. Returns True or raises ValueError."""
        self._request("GET", "v1/drivers?limit=1")
        return True

    def get_scheduled_jobs(self, start=None, end=None, limit=200):
        """Return normalized Fleetbase jobs/orders for scheduler checks.

        The live Fleetbase tenant can expose either scheduler endpoints or only
        order lists. This method tries both and normalizes the payload so the
        estimator can reason about same-day availability without caring which
        endpoint shape the tenant uses.
        """
        params = {"limit": limit}
        if start:
            params["start"] = start if isinstance(start, str) else start.isoformat()
            params["from"] = params["start"]
        if end:
            params["end"] = end if isinstance(end, str) else end.isoformat()
            params["to"] = params["end"]

        paths = [
            self._with_query("v1/scheduler/jobs", params),
            self._with_query("v1/scheduler/events", params),
            self._with_query("v1/orders", params),
        ]
        errors = []
        for path in paths:
            try:
                result = self._request("GET", path)
                items = self._coerce_collection(result)
                normalized = []
                for item in items:
                    job = self._normalize_schedule_item(item)
                    if job.get("start"):
                        normalized.append(job)
                if normalized:
                    return normalized
            except Exception as e:
                errors.append(str(e))
                _logger.info("Fleetbase schedule lookup failed for %s: %s", path, e)
        if errors:
            _logger.warning("Fleetbase scheduler lookup unavailable: %s", "; ".join(errors[:3]))
        return []

    def create_order(self, stops, scheduled_at=None, notes=None, vehicle_name=None, order_number=None):
        """Create a dispatch order in Fleetbase.

        stops: list of {type, address, lat, lng, time_window_start, time_window_end,
                         stop_date, stop_notes, liftgate, pallets}
        scheduled_at: ISO 8601 datetime string
        notes: overall trip notes
        vehicle_name: preferred vehicle name/id hint (advisory only)

        Returns: {id, tracking_number, status, public_url} or raises ValueError.
        """
        waypoints = []
        pickup = None
        dropoffs = []

        for stop in stops:
            wp = {
                "address": stop.get("address", ""),
                "location": {
                    "coordinates": [
                        float(stop.get("lng") or 0),
                        float(stop.get("lat") or 0),
                    ],
                    "type": "Point",
                },
                "type": stop.get("type", "waypoint"),
            }
            if stop.get("time_window_start"):
                wp["time_window_start"] = stop["time_window_start"]
            if stop.get("time_window_end"):
                wp["time_window_end"] = stop["time_window_end"]
            if stop.get("stop_date"):
                wp["date"] = stop["stop_date"]
            if stop.get("stop_notes"):
                wp["notes"] = stop["stop_notes"]
            if stop.get("liftgate"):
                wp["has_liftgate"] = True
            if stop.get("pallets"):
                wp["meta"] = wp.get("meta", {})
                wp["meta"]["pallets"] = stop["pallets"]

            stop_type = (stop.get("type") or "").lower()
            if stop_type == "pickup" and pickup is None:
                pickup = wp
            else:
                dropoffs.append(wp)

        payload = {
            "type": "transport",
            "payload": {
                "pickup": pickup or (waypoints[0] if waypoints else {}),
                "dropoffs": dropoffs,
            },
        }

        if scheduled_at:
            payload["scheduled_at"] = scheduled_at
        if notes:
            payload["notes"] = notes
        meta = {}
        if vehicle_name:
            meta["preferred_vehicle"] = vehicle_name
        if order_number:
            payload["order_number"] = order_number
            meta["invoice_ref"] = order_number
        if meta:
            payload["meta"] = meta

        result = self._request("POST", "v1/orders", payload)
        order = result if isinstance(result, dict) else (result[0] if result else {})
        return {
            "id": order.get("id") or order.get("public_id", ""),
            "tracking_number": order.get("tracking_number", ""),
            "status": order.get("status", ""),
            "public_url": order.get("tracking_url") or order.get("url") or "",
        }

    def get_vehicles(self):
        """Return list of vehicles from Fleetbase."""
        try:
            result = self._request("GET", "v1/fleets/vehicles")
            vehicles = result if isinstance(result, list) else result.get("data", [])
            return [
                {
                    "id": v.get("id", ""),
                    "name": v.get("name", ""),
                    "plate": v.get("plate_number", ""),
                    "status": v.get("status", ""),
                }
                for v in vehicles
            ]
        except Exception as e:
            _logger.warning("Fleetbase get_vehicles failed: %s", e)
            return []

    def get_drivers(self):
        """Return list of drivers from Fleetbase."""
        try:
            result = self._request("GET", "v1/drivers?limit=100")
            drivers = result if isinstance(result, list) else result.get("data", [])
            return [
                {
                    "id": d.get("id", ""),
                    "name": d.get("name", ""),
                    "status": d.get("status", ""),
                    "vehicle": (d.get("vehicle") or {}).get("name", ""),
                }
                for d in drivers
            ]
        except Exception as e:
            _logger.warning("Fleetbase get_drivers failed: %s", e)
            return []


    def get_order_completion(self, order_id):
        """Fetch full order data including completion proof from Fleetbase."""
        result = self._request("GET", f"v1/orders/{order_id}")
        if isinstance(result, dict) and result.get("data"):
            return result["data"]
        return result if isinstance(result, dict) else {}

    def fetch_completed_orders(self, since_date=None):
        """Return list of completed orders updated since `since_date` (datetime)."""
        params = {"status": "completed", "limit": 200}
        if since_date:
            params["updated_after"] = since_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            result = self._request("GET", self._with_query("v1/orders", params))
        except Exception as e:
            _logger.warning("fetch_completed_orders failed: %s", e)
            return []
        return self._coerce_collection(result)


def _haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0
    rlat1, rlng1, rlat2, rlng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = rlat2 - rlat1
    dlng = rlng2 - rlng1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
