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

    def get_weather_factor(self, latitude, longitude, when_dt=None, alert_level="none"):
        params = self.env["ir.config_parameter"].sudo()
        severe_multiplier = float(params.get_param("premafirm.weather.severe_multiplier", "1.25"))
        if alert_level == "severe":
            return {"factor": severe_multiplier, "condition": "severe", "api_failed": False}
        if not latitude or not longitude:
            return {"factor": 1.0, "condition": "clear", "api_failed": True}
        when_dt = when_dt or datetime.utcnow()
        hour = when_dt.strftime("%Y-%m-%dT%H:00")
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "hourly": "weathercode",
                    "forecast_days": 2,
                    "timezone": "UTC",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            times = (data.get("hourly") or {}).get("time") or []
            codes = (data.get("hourly") or {}).get("weathercode") or []
            code = 0
            if hour in times:
                idx = times.index(hour)
                code = int(codes[idx]) if idx < len(codes) else 0
            elif codes:
                code = int(codes[0])
            condition = self._condition_from_code(code)
            if condition == "rain":
                factor = 1.10
            elif condition == "snow":
                factor = 1.15
            elif condition == "severe":
                factor = severe_multiplier
            else:
                factor = 1.00
            return {"factor": factor, "condition": condition, "api_failed": False}
        except Exception:
            _logger.exception("Weather API failed")
            return {"factor": 1.0, "condition": "clear", "api_failed": True}
