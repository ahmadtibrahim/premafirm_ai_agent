"""
Trip cost calculation engine.

Uses previous calendar month's actual km driven (from fleet.daily.odometer)
to amortise fixed costs (maintenance, insurance) per km. Fuel is calculated
from GeoTab's weekly avg km/L. Driver cost uses a configurable hourly rate.
"""
import logging
from datetime import date, timedelta

_logger = logging.getLogger(__name__)


class PricingEngine:
    def __init__(self, env):
        self.env = env

    # ── Config params (admin-configurable, self-creating with defaults) ─

    def _cfg(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "fuel_price_per_l":         float(ICP.get_param("estimator.fuel_price_per_l",         "1.55")),
            "driver_rate_per_hr":       float(ICP.get_param("estimator.driver_rate_per_hr",       "40.00")),
            "margin_pct":               float(ICP.get_param("estimator.margin_pct",               "20.0")),
            # Weight surcharge: applied to load weight over the threshold
            "weight_threshold_lbs":     float(ICP.get_param("estimator.weight_threshold_lbs",     "3000")),
            "weight_surcharge_per_cwt": float(ICP.get_param("estimator.weight_surcharge_per_cwt", "5.00")),
            # Fuel load penalty: fractional increase in fuel consumption at 100% payload vs 0%
            # 0.12 means full load uses 12% more fuel than empty (applied linearly)
            "fuel_load_penalty":        float(ICP.get_param("estimator.fuel_load_penalty",        "0.12")),
        }

    # ── Previous calendar month km from daily odometer ─────────────────

    def get_prev_month_km(self, vehicle_id):
        """Return km driven in the previous calendar month.

        Queries fleet.daily.odometer for MAX-MIN odometer reading in the
        previous calendar month. Falls back to vehicle.x_monthly_avg_km
        if no odometer rows exist.

        Raises ValueError if no data is available at all.
        """
        today = date.today()
        last_of_prev = today.replace(day=1) - timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)

        self.env.cr.execute(
            """
            SELECT MAX(odometer_km) - MIN(odometer_km)
            FROM   fleet_daily_odometer
            WHERE  vehicle_id = %s
              AND  log_date  >= %s
              AND  log_date  <= %s
            """,
            (vehicle_id, first_of_prev, last_of_prev),
        )
        row = self.env.cr.fetchone()
        km = row[0] if row and row[0] is not None else 0.0

        if km > 0:
            _logger.debug(
                "PricingEngine: vehicle %s prev-month (%s) km = %.1f",
                vehicle_id, last_of_prev.strftime("%B %Y"), km,
            )
            return float(km)

        # Fallback to manually-set monthly avg
        vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
        if vehicle.x_monthly_avg_km and vehicle.x_monthly_avg_km > 0:
            _logger.info(
                "PricingEngine: no odometer rows for %s in %s, "
                "using x_monthly_avg_km=%.1f",
                vehicle.name, last_of_prev.strftime("%B %Y"), vehicle.x_monthly_avg_km,
            )
            return float(vehicle.x_monthly_avg_km)

        raise ValueError(
            f"No monthly km data available for {vehicle.name}. "
            "Either set 'Monthly Avg km (for costing)' manually on the vehicle form, "
            "or ensure GeoTab Daily Odometer Sync has run for the previous month."
        )

    # ── Full cost calculation ──────────────────────────────────────────

    def calculate(self, vehicle_id, distance_km, duration_hrs, overrides=None, load_weight_lbs=0.0):
        """Return a full cost breakdown dict for a trip of distance_km / duration_hrs.

        overrides (optional dict) can supply user-entered values that take priority:
          fuel_price_per_l      – already normalised to $/L (Gal converted by caller)
          driver_rate_per_hr    – $/hr
          insurance_monthly     – monthly insurance budget (replaces vehicle field)
          maintenance_monthly   – monthly maintenance budget (replaces vehicle field)
        Any override value of 0 / falsy → falls back to vehicle field / config default.

        load_weight_lbs is used for:
          • Fuel load factor  – heavier loads burn more fuel (linear adjustment)
          • Weight surcharge  – $/cwt for load above the configured threshold
        """
        overrides = overrides or {}
        vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
        cfg = self._cfg()

        # Fuel efficiency guard
        avg_km_l = vehicle.x_avg_km_per_l_last_week or 0.0
        if avg_km_l <= 0:
            raise ValueError(
                f"No fuel efficiency data for {vehicle.name}. "
                "Run Geotab › Weekly Fuel Average Sync or enter fuel data manually."
            )

        prev_km = self.get_prev_month_km(vehicle_id)

        # Effective cost params — user overrides take priority when > 0
        fuel_price   = overrides.get("fuel_price_per_l")   or cfg["fuel_price_per_l"]
        driver_rate  = overrides.get("driver_rate_per_hr") or cfg["driver_rate_per_hr"]
        margin_pct   = overrides.get("margin_pct")         or cfg["margin_pct"]
        ins_budget   = overrides.get("insurance_monthly")   or (vehicle.x_monthly_insurance_budget   or 0.0)
        maint_budget = overrides.get("maintenance_monthly") or (vehicle.x_monthly_maintenance_budget or 0.0)

        # ── Fuel load factor ─────────────────────────────────────────────
        # avg_km_per_l is measured across all trips (mix of loaded/empty).
        # A heavier load increases fuel consumption proportionally.
        # fuel_load_penalty (default 0.12) = extra fraction burned at 100% payload vs 0%.
        # load_factor is centred at 50% payload (no adjustment at avg load).
        max_payload = vehicle.x_max_payload_lbs or 0.0
        load_weight = float(load_weight_lbs or 0.0)
        if max_payload > 0 and load_weight > 0:
            load_ratio = min(load_weight / max_payload, 1.0)   # clamp at 100%
            # Centre around 50%: factor = 1 + (load_ratio - 0.5) * penalty
            fuel_load_factor = 1.0 + (load_ratio - 0.5) * cfg["fuel_load_penalty"]
            fuel_load_factor = max(fuel_load_factor, 0.85)     # never below 85%
        else:
            fuel_load_factor = 1.0

        # Fuel
        effective_km_l = avg_km_l / fuel_load_factor
        fuel_liters    = distance_km / effective_km_l
        fuel_cost      = fuel_liters * fuel_price

        # Fixed-cost amortisation per km (monthly budget ÷ monthly km × trip km)
        maint_per_km = maint_budget / prev_km if maint_budget and prev_km else 0.0
        ins_per_km   = ins_budget   / prev_km if ins_budget   and prev_km else 0.0
        maintenance_cost = distance_km * maint_per_km
        insurance_cost   = distance_km * ins_per_km

        # Driver
        driver_cost = duration_hrs * driver_rate

        # ── Weight surcharge ─────────────────────────────────────────────
        # Applied per 100 lbs (per CWT) of load above the threshold.
        # Configurable via Settings › Technical › System Parameters.
        weight_surcharge = 0.0
        if load_weight > cfg["weight_threshold_lbs"]:
            excess_lbs = load_weight - cfg["weight_threshold_lbs"]
            weight_surcharge = (excess_lbs / 100.0) * cfg["weight_surcharge_per_cwt"]

        total = fuel_cost + maintenance_cost + insurance_cost + driver_cost + weight_surcharge
        suggested_rate = total * (1 + margin_pct / 100.0)

        return {
            "prev_month_km":          round(prev_km, 1),
            "fuel_liters":            round(fuel_liters, 2),
            "fuel_load_factor":       round(fuel_load_factor, 3),
            "fuel_cost":              round(fuel_cost, 2),
            "maintenance_cost":       round(maintenance_cost, 2),
            "insurance_cost":         round(insurance_cost, 2),
            "driver_cost":            round(driver_cost, 2),
            "weight_surcharge":       round(weight_surcharge, 2),
            "total_cost":             round(total, 2),
            "margin_pct":             margin_pct,
            "suggested_rate":         round(suggested_rate, 2),
            "fuel_price_per_l":       round(fuel_price, 4),
            "driver_rate_per_hr":     round(driver_rate, 2),
            "avg_km_per_l":           round(avg_km_l, 2),
            "effective_km_per_l":     round(effective_km_l, 2),
            "insurance_budget":       round(ins_budget, 2),
            "maintenance_budget":     round(maint_budget, 2),
            "weight_threshold_lbs":   cfg["weight_threshold_lbs"],
            "weight_surcharge_per_cwt": cfg["weight_surcharge_per_cwt"],
            "load_weight_lbs":        round(load_weight, 0),
        }
