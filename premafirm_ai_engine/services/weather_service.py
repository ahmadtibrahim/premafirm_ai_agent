import json
import logging
from datetime import datetime

import requests

_logger = logging.getLogger(__name__)


class WeatherService:
    def __init__(self, env):
        self.env = env

    def _condition_from_code(self, code):
        if code in {71, 73, 75, 77, 85, 86}:
            return "snow"
        if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
            return "rain"
        if code in {95, 96, 99}:
            return "severe"
        return "clear"

    def get_forecast(self, lat, lon, arrival_dt):
        if not lat or not lon:
            return {"warning": "Missing coordinates", "api_failed": True}
        when_dt = arrival_dt or datetime.utcnow()
        hour_key = when_dt.strftime("%Y-%m-%dT%H:00")
        cache_key = f"premafirm.weather.{lat:.4f}.{lon:.4f}.{hour_key}"
        params = self.env["ir.config_parameter"].sudo()
        cached = params.get_param(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except (json.JSONDecodeError, TypeError, ValueError):
                _logger.warning("Invalid cached weather payload for key %s", cache_key)
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "weathercode,temperature_2m,precipitation_probability,windspeed_10m",
                    "forecast_days": 2,
                    "timezone": "UTC",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            hourly = data.get("hourly") or {}
            times = hourly.get("time") or []
            idx = times.index(hour_key) if hour_key in times else 0
            payload = {
                "time": times[idx] if times else hour_key,
                "weathercode": (hourly.get("weathercode") or [0])[idx] if hourly.get("weathercode") else 0,
                "temperature_c": (hourly.get("temperature_2m") or [None])[idx] if hourly.get("temperature_2m") else None,
                "precipitation_probability": (hourly.get("precipitation_probability") or [0])[idx] if hourly.get("precipitation_probability") else 0,
                "windspeed_kph": (hourly.get("windspeed_10m") or [0])[idx] if hourly.get("windspeed_10m") else 0,
                "api_failed": False,
            }
            params.set_param(cache_key, json.dumps(payload))
            return payload
        except Exception:
            _logger.exception("Weather API failed")
            return {"warning": "Weather API failed", "api_failed": True}

    def get_weather_factor(self, latitude, longitude, when_dt=None, alert_level="none"):
        params = self.env["ir.config_parameter"].sudo()
        severe_multiplier = float(params.get_param("premafirm.weather.severe_multiplier", "1.25"))
        if alert_level == "severe":
            return {"factor": severe_multiplier, "condition": "severe", "api_failed": False}
        forecast = self.get_forecast(latitude, longitude, when_dt or datetime.utcnow())
        if forecast.get("api_failed"):
            return {"factor": 1.0, "condition": "clear", "api_failed": True}
        code = int(forecast.get("weathercode") or 0)
        condition = self._condition_from_code(code)
        if condition == "rain":
            factor = 1.10
        elif condition == "snow":
            factor = 1.15
        elif condition == "severe":
            factor = severe_multiplier
        else:
            factor = 1.00
        return {"factor": factor, "condition": condition, "api_failed": False, "forecast": forecast}
