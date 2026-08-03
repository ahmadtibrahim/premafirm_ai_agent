"""
Trip cost calculation engine.

Fixed-cost strategy (insurance + maintenance):
  Priority 1: vehicle.x_insurance_cost_per_km / x_maintenance_cost_per_km
              These are set on the Fleet → Costing tab (monthly budget ÷ monthly avg km).
              They are the single source of truth visible on the truck form.
  Priority 2: If those fields are zero, fall back to overrides dict (user-entered in estimator)
              or: monthly_budget / prev_odometer_km (dynamic, may vary month-to-month).

Driver rate priority:
  1. vehicle.x_driver_rate_per_hr (set on Fleet → Costing tab)
  2. overrides dict (user-entered per-estimate)
  3. estimator.driver_rate_per_hr system config param

Fuel price priority:
  1. overrides dict (user-entered per-estimate or saved truck pref)
  2. estimator.fuel_price_per_l system config param
  Location to change: Settings → Technical → System Parameters → estimator.fuel_price_per_l
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
            "driver_rate_per_hr":       float(ICP.get_param("estimator.driver_rate_per_hr",       "28.00")),
            "margin_pct":               float(ICP.get_param("estimator.margin_pct",               "20.0")),
            "weight_threshold_lbs":     float(ICP.get_param("estimator.weight_threshold_lbs",     "3000")),
            "weight_surcharge_per_cwt": float(ICP.get_param("estimator.weight_surcharge_per_cwt", "5.00")),
            "fuel_load_penalty":        float(ICP.get_param("estimator.fuel_load_penalty",        "0.12")),
        }

    # ── Previous calendar month km from daily odometer ─────────────────

    def get_prev_month_km(self, vehicle_id):
        """Return km driven in the previous calendar month.

        Only used as fallback when vehicle.x_insurance_cost_per_km /
        x_maintenance_cost_per_km are zero (i.e. costing tab not filled in).

        Queries fleet.daily.odometer for MAX-MIN odometer reading in the
        previous calendar month. Falls back to vehicle.x_monthly_avg_km.
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
            return float(km)

        vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
        if vehicle.x_monthly_avg_km and vehicle.x_monthly_avg_km > 0:
            return float(vehicle.x_monthly_avg_km)

        raise ValueError(
            f"No monthly km data available for {vehicle.name}. "
            "Either set 'Monthly Avg km (for costing)' on the vehicle Costing tab, "
            "or ensure GeoTab Daily Odometer Sync has run for the previous month."
        )

    # ── Full cost calculation ──────────────────────────────────────────

    def calculate(self, vehicle_id, distance_km, duration_hrs, overrides=None, load_weight_lbs=0.0):
        """Return a full cost breakdown dict for a trip of distance_km / duration_hrs.

        Fixed costs (insurance, maintenance) resolution:
          1. vehicle.x_insurance_cost_per_km (Fleet → Costing tab) — preferred
          2. overrides["insurance_monthly"] / monthly_km — dynamic fallback
          3. vehicle.x_monthly_insurance_budget / monthly_km — last resort

        Driver rate resolution:
          1. vehicle.x_driver_rate_per_hr (Fleet → Costing tab) — preferred
          2. overrides["driver_rate_per_hr"]
          3. estimator.driver_rate_per_hr system config

        overrides (optional dict):
          fuel_price_per_l      – $/L (overrides system default)
          driver_rate_per_hr    – $/hr (overrides vehicle + config)
          insurance_monthly     – monthly budget (only used if per-km fields are 0)
          maintenance_monthly   – monthly budget (only used if per-km fields are 0)
          margin_pct            – margin %
        """
        overrides = overrides or {}
        vehicle = self.env["fleet.vehicle"].sudo().browse(vehicle_id)
        cfg = self._cfg()

        # ── Fuel efficiency guard ────────────────────────────────────────
        avg_km_l = vehicle.x_avg_km_per_l_last_week or 0.0
        if avg_km_l <= 0:
            raise ValueError(
                f"No fuel efficiency data for {vehicle.name}. "
                "Run GeoTab › Weekly Fuel Average Sync or enter fuel data manually."
            )

        # ── Fixed cost per-km: read directly from Costing tab ───────────
        # Priority 1: vehicle computed fields (shown on Costing tab)
        ins_per_km   = vehicle.x_insurance_cost_per_km   or 0.0
        maint_per_km = vehicle.x_maintenance_cost_per_km or 0.0

        # Priority 2: if per-km fields are zero, fall back to budget / prev_km
        if ins_per_km <= 0 or maint_per_km <= 0:
            ins_budget   = overrides.get("insurance_monthly")   or (vehicle.x_monthly_insurance_budget   or 0.0)
            maint_budget = overrides.get("maintenance_monthly") or (vehicle.x_monthly_maintenance_budget or 0.0)

            if ins_budget > 0 or maint_budget > 0:
                try:
                    prev_km = self.get_prev_month_km(vehicle_id)
                except ValueError as e:
                    _logger.warning("PricingEngine: %s — using 0 for per-km costs", e)
                    prev_km = 0.0

                if ins_per_km <= 0 and ins_budget and prev_km:
                    ins_per_km = ins_budget / prev_km
                if maint_per_km <= 0 and maint_budget and prev_km:
                    maint_per_km = maint_budget / prev_km

        # ── Driver rate ─────────────────────────────────────────────────
        # Priority 1: vehicle costing tab, Priority 2: override, Priority 3: system config
        driver_rate = (
            (vehicle.x_driver_rate_per_hr if vehicle.x_driver_rate_per_hr and vehicle.x_driver_rate_per_hr > 0 else 0)
            or overrides.get("driver_rate_per_hr")
            or cfg["driver_rate_per_hr"]
        )

        # ── Other cost params ────────────────────────────────────────────
        fuel_price = overrides.get("fuel_price_per_l") or cfg["fuel_price_per_l"]
        margin_pct = overrides.get("margin_pct")        or cfg["margin_pct"]

        # ── Fuel load factor ─────────────────────────────────────────────
        max_payload = vehicle.x_max_payload_lbs or 0.0
        load_weight = float(load_weight_lbs or 0.0)
        if max_payload > 0 and load_weight > 0:
            load_ratio = min(load_weight / max_payload, 1.0)
            fuel_load_factor = 1.0 + (load_ratio - 0.5) * cfg["fuel_load_penalty"]
            fuel_load_factor = max(fuel_load_factor, 0.85)
        else:
            fuel_load_factor = 1.0

        # ── Fuel cost ────────────────────────────────────────────────────
        effective_km_l = avg_km_l / fuel_load_factor
        fuel_liters    = distance_km / effective_km_l
        fuel_cost      = fuel_liters * fuel_price

        # ── Fixed costs (now using per-km values from costing tab) ───────
        maintenance_cost = distance_km * maint_per_km
        insurance_cost   = distance_km * ins_per_km

        # ── Driver cost ──────────────────────────────────────────────────
        driver_cost = duration_hrs * driver_rate

        # ── Weight surcharge ─────────────────────────────────────────────
        weight_surcharge = 0.0
        if load_weight > cfg["weight_threshold_lbs"]:
            excess_lbs = load_weight - cfg["weight_threshold_lbs"]
            weight_surcharge = (excess_lbs / 100.0) * cfg["weight_surcharge_per_cwt"]

        total = fuel_cost + maintenance_cost + insurance_cost + driver_cost + weight_surcharge
        suggested_rate = total * (1 + margin_pct / 100.0)

        # For reporting: reconstruct the monthly values shown on costing tab
        ins_monthly   = vehicle.x_monthly_insurance_budget   or round(ins_per_km   * (vehicle.x_monthly_avg_km or 0), 2)
        maint_monthly = vehicle.x_monthly_maintenance_budget or round(maint_per_km * (vehicle.x_monthly_avg_km or 0), 2)

        return {
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
            "insurance_per_km":       round(ins_per_km, 4),
            "maintenance_per_km":     round(maint_per_km, 4),
            "insurance_budget":       round(ins_monthly, 2),
            "maintenance_budget":     round(maint_monthly, 2),
            "weight_threshold_lbs":   cfg["weight_threshold_lbs"],
            "weight_surcharge_per_cwt": cfg["weight_surcharge_per_cwt"],
            "load_weight_lbs":        round(load_weight, 0),
            # Kept for backward compatibility
            "prev_month_km":          round(vehicle.x_monthly_avg_km or 0, 1),
        }
