from urllib.parse import quote

import requests


class MapboxService:
    def __init__(self, env):
        self.env = env

    def _get_api_key(self):
        return self.env["ir.config_parameter"].sudo().get_param("mapbox_api_key")

    def _geocode(self, address):
        api_key = self._get_api_key()
        if not api_key or not address:
            return None

        encoded = quote(address)
        url = (
            "https://api.mapbox.com/geocoding/v5/mapbox.places/"
            f"{encoded}.json?access_token={api_key}&limit=1"
        )
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            return None
        return features[0]["center"]

    def get_route(self, origin_address, destination_address):
        api_key = self._get_api_key()
        if not api_key:
            return {"distance_km": 0.0, "drive_hours": 0.0}

        origin = self._geocode(origin_address)
        destination = self._geocode(destination_address)
        if not origin or not destination:
            return {"distance_km": 0.0, "drive_hours": 0.0}

        coordinates = f"{origin[0]},{origin[1]};{destination[0]},{destination[1]}"
        route_url = (
            "https://api.mapbox.com/directions/v5/mapbox/driving/"
            f"{coordinates}?geometries=geojson&overview=false&access_token={api_key}"
        )
        response = requests.get(route_url, timeout=20)
        response.raise_for_status()
        routes = response.json().get("routes", [])
        if not routes:
            return {"distance_km": 0.0, "drive_hours": 0.0}

        meters = routes[0].get("distance", 0.0)
        seconds = routes[0].get("duration", 0.0)
        return {
            "distance_km": float(meters) / 1000.0,
            "drive_hours": float(seconds) / 3600.0,
        }
