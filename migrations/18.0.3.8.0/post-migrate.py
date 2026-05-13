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

    # Migrate remaining Studio dimension fields discovered after first migration
    extra_migrations = [
        ("x_studio_vehicle_height_ft_1",  "x_vehicle_height_ft"),
        ("x_studio_overall_length_ft_1",  "x_overall_length_ft"),
        ("x_studio_box_interior_height_ft", "x_box_interior_height_ft"),
    ]
    for src, dst in extra_migrations:
        if _col_exists(cr, TABLE, src) and _col_exists(cr, TABLE, dst):
            cr.execute(f"""
                UPDATE {TABLE}
                SET {dst} = {src}::numeric
                WHERE ({dst} IS NULL OR {dst} = 0)
                  AND {src} IS NOT NULL AND {src} != 0
            """)
            _logger.info("Migration: copied %s → %s on %s", src, dst, TABLE)
