import logging
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)


class MapboxService:
    ORIGIN_YARD = "5585 McAdam Rd, Mississauga ON L4Z 1P1"

    def __init__(self, env):
        self.env = env

    def _get_api_key(self):
        return self.env["ir.config_parameter"].sudo().get_param("mapbox_api_key")

    def _safe_get(self, url, timeout=20):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            _logger.exception("Mapbox request failed: %s", url)
            return {}

    def _normalize_address(self, address):
        return (address or "").strip()

    def _geocode(self, address):
        api_key = self._get_api_key()
        normalized = self._normalize_address(address)
        if not api_key or not normalized:
            return None
        encoded = quote(normalized)
        url = (
            "https://api.mapbox.com/geocoding/v5/mapbox.places/"
            f"{encoded}.json?access_token={api_key}&limit=1"
        )
        data = self._safe_get(url)
        features = data.get("features", [])
        if not features:
            return None
        return features[0].get("center")

    def _get_route_data(self, origin_address, destination_address):
        api_key = self._get_api_key()
        if not api_key:
            return {"warning": "Mapbox API key missing."}

        origin = self._geocode(origin_address)
        destination = self._geocode(destination_address)
        if not origin or not destination:
            return {"warning": "Could not geocode one or more stops."}

        coordinates = f"{origin[0]},{origin[1]};{destination[0]},{destination[1]}"
        route_url = (
            "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
            f"{coordinates}?overview=full&geometries=geojson&annotations=maxspeed,duration,distance&access_token={api_key}"
        )
        data = self._safe_get(route_url)
        routes = data.get("routes", [])
        if not routes:
            return {"warning": "No route found."}
        route = routes[0]
        route["map_url"] = (
            "https://www.mapbox.com/directions/#driving/"
            f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        )
        return route

    def _get_avg_speed(self, route):
        legs = route.get("legs", [])
        maxspeed_items = []
        for leg in legs:
            maxspeed_items.extend((leg.get("annotation") or {}).get("maxspeed", []))

        highway_count = 0
        measured_count = 0
        for item in maxspeed_items:
            speed = item.get("speed") if isinstance(item, dict) else None
            if speed:
                measured_count += 1
                if speed >= 90:
                    highway_count += 1

        highway_ratio = (highway_count / measured_count) if measured_count else 0
        return 90.0 if highway_ratio > 0.60 else 45.0

    def get_route(self, origin_address, destination_address):
        route = self._get_route_data(origin_address, destination_address)
        if route.get("warning"):
            return {"distance_km": 0.0, "drive_hours": 0.0, "warning": route["warning"]}

        meters = float(route.get("distance", 0.0))
        distance_km = meters / 1000.0
        avg_speed = self._get_avg_speed(route)
        drive_hours = (distance_km / avg_speed) if avg_speed else 0.0
        return {
            "distance_km": distance_km,
            "drive_hours": drive_hours,
            "avg_speed": avg_speed,
            "map_url": route.get("map_url"),
        }

    def calculate_trip_segments(self, stops, origin_address=None):
        origin = self._normalize_address(origin_address) or self.ORIGIN_YARD
        stop_addresses = [self._normalize_address(stop.address if hasattr(stop, "address") else stop) for stop in (stops or [])]
        stop_addresses = [address for address in stop_addresses if address]

        segments = []
        previous = origin
        for idx, address in enumerate(stop_addresses):
            route = self.get_route(previous, address)
            segment = {
                "sequence": idx + 1,
                "from": previous,
                "to": address,
                "distance_km": route.get("distance_km", 0.0),
                "drive_hours": route.get("drive_hours", 0.0),
                "avg_speed": route.get("avg_speed", 0.0),
                "warning": route.get("warning"),
                "map_url": route.get("map_url"),
            }
            segments.append(segment)
            previous = address

        # Return-to-yard leg rolled into last stop to keep stop-level totals without adding virtual stop rows.
        if stop_addresses:
            return_leg = self.get_route(previous, origin)
            if segments:
                segments[-1]["distance_km"] += return_leg.get("distance_km", 0.0)
                segments[-1]["drive_hours"] += return_leg.get("drive_hours", 0.0)
                if return_leg.get("warning"):
                    segments[-1]["warning"] = return_leg["warning"]
        return segments
