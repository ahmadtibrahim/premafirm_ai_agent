import logging
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)


class MapboxService:
    ORIGIN_YARD = "5585 McAdam Rd, Mississauga ON L4Z 1P1"

    def __init__(self, env):
        self.env = env

    def _get_api_key(self):
        key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        return key or self.env["ir.config_parameter"].sudo().get_param("mapbox_api_key")

    def _safe_get(self, url, timeout=20):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            _logger.exception("Geocoding/routing request failed: %s", url)
            return {}

    def _normalize_address(self, address):
        return (address or "").strip()

    def geocode_address(self, address):
        api_key = self._get_api_key()
        normalized = self._normalize_address(address)
        if not api_key or not normalized:
            return {"warning": "Geocoding API key missing." if not api_key else "Missing address."}

        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={quote(normalized)}&key={api_key}"
        data = self._safe_get(url)
        results = data.get("results", [])
        if not results:
            return {"warning": "Could not geocode address."}

        first = results[0]
        location = (first.get("geometry") or {}).get("location") or {}
        comps = first.get("address_components") or []
        city = None
        region = None
        postal = None
        country = None
        city_priority = ["locality", "postal_town", "sublocality", "administrative_area_level_2", "neighborhood"]
        city_rank = {name: idx for idx, name in enumerate(city_priority)}
        best_rank = 999
        for comp in comps:
            types = comp.get("types", [])
            for ctype in types:
                if ctype in city_rank and city_rank[ctype] < best_rank:
                    best_rank = city_rank[ctype]
                    city = comp.get("long_name")
            if "administrative_area_level_1" in types:
                region = comp.get("short_name")
            if "postal_code" in types:
                postal = comp.get("long_name")
            if "country" in types:
                country = comp.get("short_name")

        formatted = first.get("formatted_address") or normalized
        if not city:
            city = (formatted.split(",", 1)[0] or "").strip()

        short_address = ", ".join([x for x in [city, region] if x]).strip()
        if not short_address:
            short_address = normalized

        types = first.get("types", [])
        return {
            "latitude": location.get("lat"),
            "longitude": location.get("lng"),
            "full_address": formatted,
            "postal_code": postal,
            "country": country,
            "city": city,
            "region": region,
            "short_address": short_address,
            "place_categories": types,
        }

    def _google_maps_url(self, origin, destination):
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin['latitude']},{origin['longitude']}"
            f"&destination={destination['latitude']},{destination['longitude']}"
            "&travelmode=driving&dirflg=h"
        )

    def get_route(self, origin_address, destination_address):
        api_key = self._get_api_key()
        origin = self.geocode_address(origin_address)
        destination = self.geocode_address(destination_address)
        if origin.get("warning") or destination.get("warning"):
            return {"distance_km": 0.0, "drive_hours": 0.0, "warning": "Could not geocode one or more stops."}

        map_url = self._google_maps_url(origin, destination)
        if not api_key:
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "Routing API key missing."}

        url = (
            "https://maps.googleapis.com/maps/api/distancematrix/json"
            f"?origins={origin['latitude']},{origin['longitude']}"
            f"&destinations={destination['latitude']},{destination['longitude']}"
            f"&key={api_key}"
        )
        data = self._safe_get(url)
        rows = data.get("rows", [])
        if not rows or not rows[0].get("elements"):
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route found."}

        element = rows[0]["elements"][0]
        if element.get("status") != "OK":
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route found."}

        distance_km = float((element.get("distance") or {}).get("value", 0.0)) / 1000.0
        drive_hours = float((element.get("duration") or {}).get("value", 0.0)) / 3600.0
        return {"distance_km": distance_km, "drive_hours": drive_hours, "map_url": map_url}

    def calculate_trip_segments(self, stops, origin_address=None):
        origin = self._normalize_address(origin_address) or self.ORIGIN_YARD
        stop_addresses = []
        for stop in (stops or []):
            if hasattr(stop, "full_address") and stop.full_address:
                stop_addresses.append(self._normalize_address(stop.full_address))
            elif hasattr(stop, "address"):
                stop_addresses.append(self._normalize_address(stop.address))
            else:
                stop_addresses.append(self._normalize_address(stop))
        stop_addresses = [address for address in stop_addresses if address]

        segments = []
        previous = origin
        for idx, address in enumerate(stop_addresses):
            route = self.get_route(previous, address)
            segments.append(
                {
                    "sequence": idx + 1,
                    "from": previous,
                    "to": address,
                    "distance_km": route.get("distance_km", 0.0),
                    "drive_hours": route.get("drive_hours", 0.0),
                    "warning": route.get("warning"),
                    "map_url": route.get("map_url"),
                }
            )
            previous = address
        return segments
