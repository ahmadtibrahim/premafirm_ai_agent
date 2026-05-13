import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PremafirmGeotabDriver(models.Model):
    """Cache of Geotab driver profiles, refreshed on demand from the API."""

    _name = "premafirm.geotab.driver"
    _description = "Geotab Driver Registry"
    _order = "name"

    geotab_id = fields.Char(string="Geotab Driver ID", required=True, index=True)
    name = fields.Char(string="Full Name", required=True)
    first_name = fields.Char(string="First Name")
    last_name = fields.Char(string="Last Name")
    email = fields.Char(string="Email")
    employee_number = fields.Char(string="Employee #")
    is_driver = fields.Boolean(default=True)
    linked_contact_id = fields.Many2one(
        "res.partner",
        string="Linked Odoo Contact",
        readonly=True,
    )
    fetched_at = fields.Datetime(string="Last Fetched", readonly=True)

    _sql_constraints = [
        ("geotab_id_unique", "UNIQUE(geotab_id)", "Geotab driver ID must be unique.")
    ]

    @api.model
    def refresh_from_geotab(self):
        """Fetch all Geotab drivers and update this cache table."""
        from ..services.geotab_service import GeotabService

        cfg = self.env["ir.config_parameter"].sudo()
        if cfg.get_param("geotab.enabled", "False").lower() != "true":
            raise UserError("Geotab integration is not enabled. Enable it in Settings first.")

        try:
            svc = GeotabService(self.env)
            drivers = svc.get_drivers()
        except Exception as exc:
            raise UserError(f"Could not connect to Geotab: {exc}") from exc

        # Build map of already-linked contacts
        linked_map = {
            c.x_geotab_driver_id: c.id
            for c in self.env["res.partner"].sudo().search(
                [("x_geotab_driver_id", "!=", False)]
            )
        }

        now = fields.Datetime.now()
        fetched_ids = []

        for driver in drivers:
            d_id = (driver.get("id") or "").strip()
            first = (driver.get("firstName") or "").strip()
            last = (driver.get("lastName") or "").strip()
            full_name = f"{first} {last}".strip() or d_id
            if not d_id:
                continue

            vals = {
                "geotab_id": d_id,
                "name": full_name,
                "first_name": first or False,
                "last_name": last or False,
                "email": (driver.get("email") or "").strip() or False,
                "employee_number": (driver.get("employeeNo") or "").strip() or False,
                "is_driver": True,
                "linked_contact_id": linked_map.get(d_id, False),
                "fetched_at": now,
            }
            existing = self.search([("geotab_id", "=", d_id)], limit=1)
            if existing:
                existing.write(vals)
                fetched_ids.append(existing.id)
            else:
                rec = self.create(vals)
                fetched_ids.append(rec.id)

        _logger.info("Geotab driver cache refreshed: %d drivers", len(fetched_ids))
        return len(fetched_ids)
