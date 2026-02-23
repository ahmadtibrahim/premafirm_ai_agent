import hashlib
import logging
import math
import time
from datetime import datetime
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)


class MapboxService:
    ORIGIN_YARD = "5585 McAdam Rd, Mississauga ON L4Z 1P1"

    def __init__(self, env):
        self.env = env
        self._memory_cache = {}


    def _get_cache_model(self):
        try:
            return self.env["premafirm.mapbox.cache"]
        except Exception:
            return None

    def _cache_lookup(self, origin, destination, waypoints_hash="", departure_dt=None):
        departure_dt = departure_dt or datetime.utcnow()
        rounded_hour = departure_dt.replace(minute=0, second=0, microsecond=0)
        memory_key = (origin, destination, waypoints_hash or "", rounded_hour.isoformat())
        if memory_key in self._memory_cache:
            return self._memory_cache[memory_key]

        cache_model = self._get_cache_model()
        if not cache_model:
            return None
        rec = cache_model.search([
            ("origin", "=", origin),
            ("destination", "=", destination),
            ("waypoints_hash", "=", waypoints_hash or ""),
            ("departure_date", "=", rounded_hour),
        ], limit=1)
        if not rec:
            return None
        payload = {
            "distance_km": rec.distance_km,
            "drive_minutes": rec.duration_minutes,
            "polyline": rec.polyline,
            "warning": False,
        }
        self._memory_cache[memory_key] = payload
        return payload

    def _cache_store(self, origin, destination, waypoints_hash, departure_dt, distance_km, duration_minutes, polyline):
        departure_dt = departure_dt or datetime.utcnow()
        rounded_hour = departure_dt.replace(minute=0, second=0, microsecond=0)
        memory_key = (origin, destination, waypoints_hash or "", rounded_hour.isoformat())
        payload = {
            "distance_km": float(distance_km or 0.0),
            "drive_minutes": float(duration_minutes or 0.0),
            "polyline": polyline or "",
            "warning": False,
        }
        self._memory_cache[memory_key] = payload

        cache_model = self._get_cache_model()
        if not cache_model:
            return
        vals = {
            "origin": origin,
            "destination": destination,
            "waypoints_hash": waypoints_hash or "",
            "departure_date": rounded_hour,
            "distance_km": payload["distance_km"],
            "duration_minutes": payload["drive_minutes"],
            "polyline": payload["polyline"],
        }
        rec = cache_model.search([
            ("origin", "=", origin),
            ("destination", "=", destination),
            ("waypoints_hash", "=", vals["waypoints_hash"]),
            ("departure_date", "=", vals["departure_date"]),
        ], limit=1)
        if rec:
            rec.write(vals)
        else:
            cache_model.create(vals)

    def _get_api_key(self):
        params = self.env["ir.config_parameter"].sudo()
        return (
            params.get_param("mapbox.access_token")
            or params.get_param("mapbox_api_key")
            or params.get_param("google_maps_api_key")
        )

    def _safe_get(self, url, timeout=20):
        wait = 0.5
        for attempt in range(3):
            try:
                response = requests.get(url, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except Exception:
                if attempt == 2:
                    _logger.exception("Geocoding/routing request failed: %s", url)
                    return {}
                time.sleep(wait)
                wait *= 2
        return {}

    def _normalize_address(self, address):
        return (address or "").strip()

    def geocode_address(self, address):
        api_key = self._get_api_key()
        normalized = self._normalize_address(address)
        if not api_key or not normalized:
            return {"warning": "Mapbox access token missing." if not api_key else "Missing address."}

        url = (
            "https://api.mapbox.com/geocoding/v5/mapbox.places/"
            f"{quote(normalized)}.json?access_token={api_key}&autocomplete=true&limit=1"
        )
        data = self._safe_get(url)
        features = data.get("features", [])
        if not features:
            return {"warning": "Could not geocode address."}

        first = features[0]
        center = first.get("center") or [None, None]
        context = first.get("context") or []
        city = None
        region = None
        postal = None
        country_code = None
        for item in context:
            item_id = item.get("id", "")
            if item_id.startswith("place"):
                city = item.get("text")
            elif item_id.startswith("region"):
                region = item.get("short_code", "").upper().replace("CA-", "").replace("US-", "") or item.get("text")
            elif item_id.startswith("postcode"):
                postal = item.get("text")
            elif item_id.startswith("country"):
                country_code = (item.get("short_code") or "").upper()

        place_name = first.get("place_name") or normalized
        if not city:
            city = (first.get("text") or "").strip() or (place_name.split(",", 1)[0] or "").strip()
        short_address = ", ".join([x for x in [city, region] if x]).strip() or normalized

        return {
            "latitude": center[1],
            "longitude": center[0],
            "full_address": place_name,
            "postal_code": postal,
            "country": country_code,
            "country_code": country_code,
            "city": city,
            "region": region,
            "short_address": short_address,
            "place_categories": first.get("place_type") or [],
        }


    def _directions_for_coordinates(self, coordinates):
        api_key = self._get_api_key()
        if not api_key:
            return {}
        joined = ";".join(f"{lon},{lat}" for lon, lat in coordinates if lat is not None and lon is not None)
        if ";" not in joined:
            return {}
        url = (
            "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
            f"{joined}?access_token={api_key}&overview=full&steps=false&annotations=duration,distance&geometries=geojson"
        )
        return self._safe_get(url)

    def _google_maps_url(self, origin, destination):
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin['latitude']},{origin['longitude']}"
            f"&destination={destination['latitude']},{destination['longitude']}"
            "&travelmode=driving"
        )

    @staticmethod
    def _haversine_km(lat1, lon1, lat2, lon2):
        r = 6371.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        d1 = math.radians(lat2 - lat1)
        d2 = math.radians(lon2 - lon1)
        a = math.sin(d1 / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d2 / 2) ** 2
        return r * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

    def get_route(self, origin_address, destination_address):
        api_key = self._get_api_key()
        origin = self.geocode_address(origin_address)
        destination = self.geocode_address(destination_address)
        if origin.get("warning") or destination.get("warning"):
            return {"distance_km": 0.0, "drive_hours": 0.0, "warning": "Could not geocode one or more stops."}

        map_url = self._google_maps_url(origin, destination)
        if not api_key:
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "Routing API key missing."}

        coords = f"{origin['longitude']},{origin['latitude']};{destination['longitude']},{destination['latitude']}"
        url = (
            "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
            f"{coords}?access_token={api_key}&overview=full&steps=false&annotations=duration,distance&geometries=geojson"
        )
        data = self._safe_get(url)
        routes = data.get("routes") or []
        if not routes:
            try:
                distance_km = self._haversine_km(origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"])
                return {"distance_km": distance_km, "drive_hours": max(distance_km / 60.0, 0.0), "map_url": map_url, "warning": "Mapbox route unavailable; used fallback estimate."}
            except Exception:
                return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route found."}

        route = routes[0]
        legs = route.get("legs") or []
        if not legs:
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route legs found."}

        distance_km = float(sum(float(leg.get("distance") or 0.0) for leg in legs)) / 1000.0
        drive_hours = float(sum(float(leg.get("duration") or 0.0) for leg in legs)) / 3600.0
        return {"distance_km": distance_km, "drive_hours": drive_hours, "map_url": map_url}


    def get_travel_time(self, origin, destination):
        origin_n = self._normalize_address(origin)
        destination_n = self._normalize_address(destination)
        cached = self._cache_lookup(origin_n, destination_n, waypoints_hash="", departure_dt=datetime.utcnow())
        if cached:
            return {
                "distance_km": float(cached.get("distance_km") or 0.0),
                "drive_minutes": float(cached.get("drive_minutes") or 0.0),
                "map_url": None,
                "warning": False,
            }
        route = self.get_route(origin_n, destination_n)
        distance_km = float(route.get("distance_km") or 0.0)
        drive_minutes = float(route.get("drive_hours") or 0.0) * 60.0
        if distance_km or drive_minutes:
            self._cache_store(origin_n, destination_n, "", datetime.utcnow(), distance_km, drive_minutes, "")
        return {
            "distance_km": distance_km,
            "drive_minutes": drive_minutes,
            "map_url": route.get("map_url"),
            "warning": route.get("warning"),
        }

    def calculate_trip_segments(self, origin, stops, return_home=True):
        origin_address = self._normalize_address(origin) or self.ORIGIN_YARD
        stop_list = list(stops or [])
        addresses = [origin_address]
        addresses.extend(self._normalize_address(getattr(stop, "full_address", False) or getattr(stop, "address", stop)) for stop in stop_list)
        if return_home:
            addresses.append(origin_address)

        segments = []
        for idx in range(len(addresses) - 1):
            from_addr = addresses[idx]
            to_addr = addresses[idx + 1]
            travel = self.get_travel_time(from_addr, to_addr)
            waypoints_hash = hashlib.sha1(f"{from_addr}|{to_addr}".encode("utf-8")).hexdigest()
            if travel.get("distance_km") or travel.get("drive_minutes"):
                self._cache_store(from_addr, to_addr, waypoints_hash, datetime.utcnow(), travel.get("distance_km"), travel.get("drive_minutes"), "")
            segments.append(
                {
                    "sequence": idx + 1,
                    "from": from_addr,
                    "to": to_addr,
                    "distance_km": float(travel.get("distance_km") or 0.0),
                    "duration_minutes": float(travel.get("drive_minutes") or 0.0),
                    "drive_hours": float(travel.get("drive_minutes") or 0.0) / 60.0,
                    "polyline": "",
                    "warning": travel.get("warning"),
                    "map_url": travel.get("map_url"),
                }
            )
        return segments
