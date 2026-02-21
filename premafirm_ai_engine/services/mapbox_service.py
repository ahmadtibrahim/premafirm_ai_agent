import logging
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)


class MapboxService:
    ORIGIN_YARD = "5585 McAdam Rd, Mississauga ON L4Z 1P1"

    def __init__(self, env):
        self.env = env

    def _get_api_key(self):
        params = self.env["ir.config_parameter"].sudo()
        return (
            params.get_param("mapbox.access_token")
            or params.get_param("mapbox_api_key")
            or params.get_param("google_maps_api_key")
        )

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
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route found."}

        route = routes[0]
        legs = route.get("legs") or []
        if not legs:
            return {"distance_km": 0.0, "drive_hours": 0.0, "map_url": map_url, "warning": "No route legs found."}

        distance_km = float(sum(float(leg.get("distance") or 0.0) for leg in legs)) / 1000.0
        drive_hours = float(sum(float(leg.get("duration") or 0.0) for leg in legs)) / 3600.0
        return {"distance_km": distance_km, "drive_hours": drive_hours, "map_url": map_url}


    def get_travel_time(self, origin, destination):
        route = self.get_route(origin, destination)
        return {
            "distance_km": float(route.get("distance_km") or 0.0),
            "drive_minutes": float(route.get("drive_hours") or 0.0) * 60.0,
            "map_url": route.get("map_url"),
            "warning": route.get("warning"),
        }

    def calculate_trip_segments(self, stops, origin_address=None):
        origin_address = self._normalize_address(origin_address) or self.ORIGIN_YARD
        origin_geo = self.geocode_address(origin_address)

        stop_points = []
        for stop in (stops or []):
            if hasattr(stop, "latitude") and hasattr(stop, "longitude") and stop.latitude and stop.longitude:
                stop_points.append(
                    {
                        "address": self._normalize_address(getattr(stop, "full_address", False) or getattr(stop, "address", "")),
                        "latitude": float(stop.latitude),
                        "longitude": float(stop.longitude),
                    }
                )
                continue

            address = self._normalize_address(getattr(stop, "full_address", False) or getattr(stop, "address", stop))
            geo = self.geocode_address(address)
            stop_points.append(
                {
                    "address": address,
                    "latitude": geo.get("latitude"),
                    "longitude": geo.get("longitude"),
                    "warning": geo.get("warning"),
                }
            )

        if not stop_points:
            return []

        coordinates = []
        if origin_geo.get("latitude") and origin_geo.get("longitude"):
            coordinates.append((origin_geo["longitude"], origin_geo["latitude"]))
        else:
            coordinates.append((None, None))
        coordinates.extend((pt.get("longitude"), pt.get("latitude")) for pt in stop_points)

        directions = self._directions_for_coordinates(coordinates)
        legs = ((directions.get("routes") or [{}])[0].get("legs") or []) if directions else []

        segments = []
        previous_label = origin_address
        for idx, point in enumerate(stop_points):
            leg = legs[idx] if idx < len(legs) else {}
            origin = {
                "latitude": coordinates[idx][1],
                "longitude": coordinates[idx][0],
            }
            destination = {
                "latitude": point.get("latitude"),
                "longitude": point.get("longitude"),
            }
            map_url = None
            if origin["latitude"] is not None and destination["latitude"] is not None:
                map_url = self._google_maps_url(origin, destination)

            drive_hours = float(leg.get("duration") or 0.0) / 3600.0

            warning = point.get("warning")
            if not leg and not warning:
                warning = "No route found."

            segments.append(
                {
                    "sequence": idx + 1,
                    "from": previous_label,
                    "to": point.get("address"),
                    "distance_km": float(leg.get("distance") or 0.0) / 1000.0,
                    "drive_hours": drive_hours,
                    "warning": warning,
                    "map_url": map_url,
                }
            )
            previous_label = point.get("address")
        return segments
