"""
MapBox service — geocoding and truck-profile route calculation.

Existing callers: geotab_sync.py uses MapboxService(env).reverse_geocode(lat, lng)
New callers:      rate_estimator.py uses geocode_address() and get_route()
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

_logger = logging.getLogger(__name__)

_BASE_GEO = "https://api.mapbox.com/geocoding/v5/mapbox.places"
_BASE_DIR = "https://api.mapbox.com/directions/v5/mapbox/driving"


class MapboxService:
    def __init__(self, env):
        self.env = env
        self._cached_token = None

    # ── Token ──────────────────────────────────────────────────────────

    def _token(self):
        if not self._cached_token:
            ICP = self.env["ir.config_parameter"].sudo()
            self._cached_token = (
                ICP.get_param("mapbox.access_token")
                or ICP.get_param("mapbox_api_key")
                or ""
            )
        if not self._cached_token:
            raise ValueError(
                "MapBox token not configured. "
                "Set mapbox.access_token in Settings › Technical › System Parameters."
            )
        return self._cached_token

    def _get(self, url, params):
        params["access_token"] = self._token()
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full_url, headers={"User-Agent": "Odoo/18"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            raise ValueError(f"HTTP {e.code}: {body or e.reason}") from e

    # ── Reverse geocode ────────────────────────────────────────────────

    def reverse_geocode(self, lat, lng):
        """Return a human-readable address string for lat/lng.
        Called by geotab_sync.py — signature must stay stable."""
        try:
            data = self._get(
                f"{_BASE_GEO}/{lng},{lat}.json",
                {"types": "address,place", "limit": 1},
            )
            features = data.get("features") or []
            if features:
                return features[0].get("place_name", "")
        except Exception as e:
            _logger.warning("MapBox reverse_geocode failed (%s,%s): %s", lat, lng, e)
        return ""

    # ── Forward geocode (address autocomplete) ─────────────────────────

    def geocode_address(self, query, country="ca,us"):
        """Return up to 5 suggestions for a typed address string.
        Each result: {place_name, lat, lng}
        """
        if not query or len(query) < 2:
            return []
        try:
            data = self._get(
                f"{_BASE_GEO}/{urllib.parse.quote(query)}.json",
                {
                    "country": country,
                    "types": "address,place,locality,neighborhood,postcode",
                    "autocomplete": "true",
                    "limit": 5,
                },
            )
            results = []
            for f in data.get("features") or []:
                center = f.get("center") or []
                if len(center) >= 2:
                    results.append({
                        "place_name": f.get("place_name", ""),
                        "lng": center[0],
                        "lat": center[1],
                    })
            return results
        except Exception as e:
            _logger.warning("MapBox geocode_address failed for %r: %s", query, e)
            return []

    # ── Directions (truck-profile route) ──────────────────────────────

    def get_route(
        self,
        origin_lng,
        origin_lat,
        dest_lng,
        dest_lat,
        max_height_ft=0.0,
        gvwr_lbs=0.0,
        allow_cross_border=True,
        avoid_tolls=False,
    ):
        """Two-point route — kept for backward compatibility (geotab_sync etc.)."""
        return self.get_route_multi(
            [{"lng": origin_lng, "lat": origin_lat}, {"lng": dest_lng, "lat": dest_lat}],
            max_height_ft=max_height_ft,
            gvwr_lbs=gvwr_lbs,
            allow_cross_border=allow_cross_border,
            avoid_tolls=avoid_tolls,
        )

    def get_route_multi(
        self,
        waypoints,
        max_height_ft=0.0,
        gvwr_lbs=0.0,
        allow_cross_border=True,
        avoid_tolls=False,
    ):
        """Multi-stop route calculation.

        waypoints: list of {lng, lat} dicts, minimum 2 entries.
        Truck dimensions steer MapBox away from restricted bridges/tunnels.
        allow_cross_border=False adds exclude=country_borders.
        avoid_tolls=True adds exclude=toll to avoid toll roads.

        Returns: {distance_km, duration_hrs, geometry}
        Raises ValueError if no route can be found.
        """
        if len(waypoints) < 2:
            raise ValueError("At least two waypoints are required for routing.")

        coords = ";".join(f"{w['lng']},{w['lat']}" for w in waypoints)
        base_params = {
            "geometries": "geojson",
            "overview": "simplified",
            "steps": "false",
        }
        attempt_variants = []

        full_params = dict(base_params)
        if max_height_ft and max_height_ft > 0:
            # Standard Mapbox driving profiles may reject truck-specific params.
            full_params["max_height"] = round(max_height_ft / 3.28084, 2)
        if gvwr_lbs and gvwr_lbs > 0:
            full_params["max_weight"] = round(gvwr_lbs / 2204.62, 3)

        excludes = []
        if not allow_cross_border:
            excludes.append("country_borders")
        if avoid_tolls:
            excludes.append("toll")
        if excludes:
            full_params["exclude"] = ",".join(excludes)
        attempt_variants.append(("full", full_params))

        if "max_height" in full_params or "max_weight" in full_params:
            no_truck_limits = dict(full_params)
            no_truck_limits.pop("max_height", None)
            no_truck_limits.pop("max_weight", None)
            attempt_variants.append(("no_truck_limits", no_truck_limits))

        if not allow_cross_border and excludes:
            no_border_exclude = dict(base_params)
            if avoid_tolls:
                no_border_exclude["exclude"] = "toll"
            attempt_variants.append(("no_country_borders", no_border_exclude))

        if avoid_tolls:
            toll_only = dict(base_params)
            toll_only["exclude"] = "toll"
            attempt_variants.append(("toll_only", toll_only))

        attempt_variants.append(("core", dict(base_params)))
        data = None
        errors = []
        for label, params in attempt_variants:
            try:
                data = self._get(f"{_BASE_DIR}/{coords}", dict(params))
                if label != "full":
                    _logger.warning(
                        "MapBox directions fallback succeeded with '%s' params for %s waypoints.",
                        label,
                        len(waypoints),
                    )
                break
            except Exception as e:
                errors.append(f"{label}: {e}")
                _logger.warning("MapBox directions attempt '%s' failed: %s", label, e)

        if data is None:
            detail = errors[-1] if errors else "unknown error"
            raise ValueError(f"MapBox Directions API error: {detail}")

        routes = data.get("routes") or []
        if not routes:
            if not allow_cross_border:
                raise ValueError(
                    "No truck-legal route found within Canada. "
                    "Try enabling 'Allow USA Routing' for a cross-border path."
                )
            raise ValueError(
                "No route found between the selected locations."
            )

        route = routes[0]
        legs = [
            {
                "distance_km": round((leg.get("distance") or 0) / 1000, 2),
                "duration_hrs": round((leg.get("duration") or 0) / 3600, 2),
            }
            for leg in (route.get("legs") or [])
        ]
        return {
            "distance_km": round((route.get("distance") or 0) / 1000, 2),
            "duration_hrs": round((route.get("duration") or 0) / 3600, 2),
            "geometry": route.get("geometry"),
            "legs": legs,
        }
