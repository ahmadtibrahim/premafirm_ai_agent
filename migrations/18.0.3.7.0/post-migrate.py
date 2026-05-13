import logging
from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _col_exists(cr, table, col):
    cr.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, col),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    TABLE = "fleet_vehicle"

    # Copy Studio integer values → module float/int fields (only where destination is empty)
    int_migrations = [
        ("x_studio_gvwr_lbs",         "x_gvwr_lbs"),
        ("x_studio_max_pallets_1",     "x_max_pallets"),
        ("x_studio_payload_limit_lbs", "x_max_payload_lbs"),
    ]
    for src, dst in int_migrations:
        if _col_exists(cr, TABLE, src) and _col_exists(cr, TABLE, dst):
            cr.execute(f"""
                UPDATE {TABLE}
                SET {dst} = {src}::numeric
                WHERE ({dst} IS NULL OR {dst} = 0)
                  AND {src} IS NOT NULL AND {src} != 0
            """)
            _logger.info("Migration: copied %s → %s on %s", src, dst, TABLE)

    # Tank capacity: gallons → liters (1 US gal = 3.78541 L)
    if _col_exists(cr, TABLE, "x_studio_fuel_tank_capacity_gal") and _col_exists(cr, TABLE, "x_tank_capacity_l"):
        cr.execute(f"""
            UPDATE {TABLE}
            SET x_tank_capacity_l = x_studio_fuel_tank_capacity_gal * 3.78541
            WHERE (x_tank_capacity_l IS NULL OR x_tank_capacity_l = 0)
              AND x_studio_fuel_tank_capacity_gal IS NOT NULL
              AND x_studio_fuel_tank_capacity_gal != 0
        """)
        _logger.info("Migration: copied x_studio_fuel_tank_capacity_gal → x_tank_capacity_l (gal→L)")

    # Deactivate Studio views for fleet.vehicle that inject duplicate x_studio_* fields
    env = api.Environment(cr, SUPERUSER_ID, {})
    STUDIO_MARKERS = (
        "x_studio_gvwr_lbs",
        "x_studio_max_pallets_1",
        "x_studio_payload_limit_lbs",
    )
    views = env["ir.ui.view"].sudo().search([
        ("model", "=", "fleet.vehicle"),
        ("active", "=", True),
    ])
    for v in views:
        arch = v.arch_db or ""
        if any(m in arch for m in STUDIO_MARKERS):
            v.active = False
            _logger.info("Migration: deactivated Studio view id=%s name=%s", v.id, v.name)
