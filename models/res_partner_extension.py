import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_CLEARBIT_SUGGEST = "https://autocomplete.clearbit.com/v1/companies/suggest"


def _normalize_url(raw):
    """Ensure URL has https:// prefix."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        return f"https://{raw}"
    return raw


def _domain_to_name(domain):
    """Derive a display name from a bare domain (walmart.com → Walmart)."""
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Legacy field (preserved)
    is_driver = fields.Boolean(default=False)

    # ------------------------------------------------------------------
    # Company tag inheritance — additive propagation to child contacts,
    # Province/City auto-tagging, and mirroring onto related CRM leads
    # ------------------------------------------------------------------

    @api.model
    def _get_or_create_category(self, name):
        name = (name or "").strip()
        if not name:
            return self.env["res.partner.category"]
        Category = self.env["res.partner.category"]
        category = Category.search([("name", "=", name)], limit=1)
        if not category:
            category = Category.create({"name": name})
        return category

    def _sync_location_tags(self):
        """Ensure each company has a Contact Tag for its Province/State and its City."""
        for partner in self:
            if not partner.is_company:
                continue
            names = []
            if partner.state_id and partner.state_id.name:
                names.append(partner.state_id.name.strip())
            if partner.city and partner.city.strip():
                names.append(partner.city.strip())
            if not names:
                continue
            category_ids = [self._get_or_create_category(name).id for name in names]
            missing = set(category_ids) - set(partner.category_id.ids)
            if missing:
                partner.write({"category_id": [(4, cid) for cid in missing]})

    def _sync_crm_lead_tags(self):
        """Mirror this partner's Contact Tags onto CRM Tags of its related leads."""
        if not self:
            return
        leads = self.env["crm.lead"].search([("partner_id", "in", self.ids)])
        if leads:
            leads._sync_tags_from_partner()

    @api.model
    def _resolve_contact_parent_company(self, parent):
        current = parent
        company = self.env["res.partner"]
        visited = set()
        while current and current.id and current.id not in visited:
            visited.add(current.id)
            if current.is_company:
                company = current
            current = current.parent_id
        return company

    @api.model
    def _normalize_parent_company_vals(self, vals, partner=None):
        vals = dict(vals)
        is_company = vals.get("is_company")
        if is_company is None and partner:
            is_company = partner.is_company
        if is_company:
            return vals

        parent_id = vals.get("parent_id")
        if not parent_id:
            return vals

        parent = self.browse(parent_id)
        company = self._resolve_contact_parent_company(parent)
        if company:
            vals["parent_id"] = company.id
        return vals

    def write(self, vals):
        if "parent_id" in vals or "is_company" in vals:
            vals = self._normalize_parent_company_vals(vals, partner=self[:1] if len(self) == 1 else None)
        tags_changing = "category_id" in vals
        loc_changing = "state_id" in vals or "city" in vals
        companies_before = (
            self.filtered("is_company") if (tags_changing or loc_changing) else self.browse()
        )

        result = super().write(vals)

        if loc_changing and companies_before:
            companies_before._sync_location_tags()

        if tags_changing:
            for company in companies_before:
                if company.child_ids and company.category_id:
                    company.child_ids.write({
                        "category_id": [(4, tid) for tid in company.category_id.ids]
                    })

        if tags_changing or loc_changing:
            self._sync_crm_lead_tags()

        return result

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._normalize_parent_company_vals(vals) for vals in vals_list]
        records = super().create(vals_list)
        companies = records.filtered("is_company")
        if companies:
            companies._sync_location_tags()
        for partner in records:
            if (
                not partner.is_company
                and partner.parent_id
                and partner.parent_id.is_company
                and partner.parent_id.category_id
            ):
                partner.write({
                    "category_id": [(4, tid) for tid in partner.parent_id.category_id.ids]
                })
        return records

    # ------------------------------------------------------------------
    # Driver profile detection (computed from "Driver" tag)
    # ------------------------------------------------------------------

    x_is_driver_profile = fields.Boolean(
        string="Is Driver Profile",
        compute="_compute_is_driver_profile",
        store=True,
        index=True,
    )

    @api.depends("category_id.name")
    def _compute_is_driver_profile(self):
        for partner in self:
            partner.x_is_driver_profile = any(
                (cat.name or "").strip().lower() == "driver"
                for cat in partner.category_id
            )

    # ------------------------------------------------------------------
    # Identity / Geotab Link
    # ------------------------------------------------------------------

    x_geotab_driver_id = fields.Char(string="Geotab Driver ID", index=True)
    x_geotab_driver_key = fields.Char(string="Geotab Driver Key")
    x_last_driver_sync_at = fields.Datetime(string="Last Driver Sync")
    x_driver_sync_status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("pending", "Pending"), ("unmatched", "Unmatched")],
        string="Driver Sync Status",
    )
    x_driver_sync_error = fields.Text(string="Driver Sync Error")

    # ------------------------------------------------------------------
    # Driver Profile Info
    # ------------------------------------------------------------------

    x_driver_license_number = fields.Char(string="Driver License #")
    x_driver_status = fields.Selection(
        [("active", "Active"), ("inactive", "Inactive"), ("on_leave", "On Leave")],
        string="Driver Status",
        default="active",
    )
    x_driver_timezone = fields.Char(string="Driver Timezone", default="America/Toronto")
    x_driver_home_terminal = fields.Char(string="Home Terminal")
    x_driver_hire_date = fields.Date(string="Hire Date")
    x_driver_active = fields.Boolean(string="Driver Active", default=True)

    # ------------------------------------------------------------------
    # ELD / Log / Work Data — daily/weekly summaries
    # ------------------------------------------------------------------

    x_last_known_vehicle_id = fields.Many2one("fleet.vehicle", string="Last Known Vehicle")
    x_last_duty_status = fields.Selection(
        [("D", "Driving"), ("ON", "On Duty"), ("OFF", "Off Duty"), ("SB", "Sleeper Berth")],
        string="Last Duty Status",
    )
    x_last_log_sync_at = fields.Datetime(string="Last Log Sync")
    x_last_shift_start_at = fields.Datetime(string="Last Shift Start")
    x_last_shift_end_at = fields.Datetime(string="Last Shift End")

    x_today_driving_hours = fields.Float(string="Today Driving Hours", digits=(6, 2))
    x_today_on_duty_hours = fields.Float(string="Today On-Duty Hours", digits=(6, 2))
    x_today_idle_hours = fields.Float(string="Today Idle Hours", digits=(6, 2))
    x_today_distance_km = fields.Float(string="Today Distance (km)", digits=(8, 1))

    x_week_driving_hours = fields.Float(string="Week Driving Hours", digits=(6, 2))
    x_week_on_duty_hours = fields.Float(string="Week On-Duty Hours", digits=(6, 2))
    x_week_distance_km = fields.Float(string="Week Distance (km)", digits=(8, 1))

    # ------------------------------------------------------------------
    # Smart button counts
    # ------------------------------------------------------------------

    x_driver_log_count = fields.Integer(
        string="Driver Log Count",
        compute="_compute_driver_log_count",
    )
    x_driver_assignment_count = fields.Integer(
        string="Assignment Count",
        compute="_compute_driver_assignment_count",
    )

    def _compute_driver_log_count(self):
        for partner in self:
            partner.x_driver_log_count = self.env["fleet.driver.log"].search_count(
                [("driver_contact_id", "=", partner.id)]
            )

    def _compute_driver_assignment_count(self):
        for partner in self:
            partner.x_driver_assignment_count = self.env["fleet.driver.assignment"].search_count(
                [("driver_contact_id", "=", partner.id)]
            )

    def action_open_geotab_driver_link_wizard(self):
        """Open wizard to select a Geotab driver profile and link to this contact."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Link to Geotab Driver",
            "res_model": "contact.geotab.link.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_contact_id": self.id},
        }

    def action_open_driver_logs(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Driver Logs",
            "res_model": "fleet.driver.log",
            "view_mode": "tree",
            "domain": [("driver_contact_id", "=", self.id)],
            "context": {"default_driver_contact_id": self.id},
        }

    def action_open_driver_assignments(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Driver Assignments",
            "res_model": "fleet.driver.assignment",
            "view_mode": "tree",
            "domain": [("driver_contact_id", "=", self.id)],
            "context": {"default_driver_contact_id": self.id},
        }

    def init(self):
        self.env.cr.execute(
            "ALTER TABLE res_partner "
            "ADD COLUMN IF NOT EXISTS is_driver boolean DEFAULT false"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner "
            "ADD COLUMN IF NOT EXISTS x_is_driver_profile boolean DEFAULT false"
        )

    # ------------------------------------------------------------------
    # Partner autocomplete — replace IAP (credits) with Clearbit free
    # suggest API (Snov.io enrichment removed 2026-08-18 — subscription
    # cancelled)
    # ------------------------------------------------------------------

    @api.model
    def autocomplete_by_name(self, query, query_country_id, timeout=15):
        """Use Clearbit free suggest API instead of IAP DNB (zero credits).

        Sets duns = domain on each suggestion so our enrich_by_duns override
        pre-fills the new-partner form without any paid API.
        """
        query = (query or "").strip()
        if len(query) < 2:
            return []
        try:
            resp = requests.get(
                _CLEARBIT_SUGGEST,
                params={"query": query},
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json() or []
        except Exception:
            _logger.warning("Clearbit suggest failed for %r, returning empty", query)
            return []

        results = []
        for item in raw[:10]:
            name = (item.get("name") or "").strip()
            domain = (item.get("domain") or "").strip()
            if not name:
                continue
            results.append({
                "name": name,
                "domain": domain,
                # Pass domain as duns so enrich_by_duns below receives it
                "duns": domain,
            })
        return results

    @api.model
    def enrich_by_duns(self, duns, timeout=15):
        """If duns is a domain (set by our autocomplete_by_name), pre-fill the
        new-partner form from Clearbit + domain. If it's a real DUNS number,
        skip to avoid consuming IAP credits. Always returns at minimum
        {name, website}.
        """
        if not duns or not isinstance(duns, str):
            return {}

        is_domain = (
            "." in duns
            and " " not in duns
            and not duns.replace("-", "").replace(".", "").isdigit()
            and len(duns) < 200
        )
        if not is_domain:
            _logger.info("enrich_by_duns: skipping real DUNS %r (no IAP credits)", duns)
            return {}

        # Snov.io enrichment removed 2026-08-18 (subscription cancelled) —
        # build the minimum viable result from Clearbit + domain.
        name = self._clearbit_name_for_domain(duns) or _domain_to_name(duns)
        website = _normalize_url(duns)
        _logger.info("enrich_by_duns: pre-filling from Clearbit name=%r", name)
        return {"name": name, "website": website}

    def _clearbit_name_for_domain(self, domain):
        """Call Clearbit suggest to find the canonical company name for a domain."""
        try:
            resp = requests.get(
                _CLEARBIT_SUGGEST,
                params={"query": domain},
                timeout=5,
            )
            resp.raise_for_status()
            items = resp.json() or []
            # Exact domain match first
            for item in items:
                if (item.get("domain") or "").lower() == domain.lower() and item.get("name"):
                    return item["name"]
            # Accept first result as fallback
            if items and items[0].get("name"):
                return items[0]["name"]
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Google Places / Nominatim — find or create partner from place data
    # ------------------------------------------------------------------

    @api.model
    def find_or_create_from_google_place(self, name, phone=False, website=False,
                                          street=False, city=False, zip_code=False,
                                          state_code=False, country_code=False):
        """Find an existing company partner by name, or create a new one.

        Called by the google_partner_m2o JS widget when a user selects a
        Google Places / Nominatim suggestion on the partner_id many2one field.

        Returns {'id': int, 'display_name': str}.
        """
        name = (name or "").strip()
        if not name:
            return {}

        existing = self.search(
            [("name", "=ilike", name), ("is_company", "=", True)],
            limit=1,
        )
        if existing:
            vals = {}
            if phone   and not existing.phone:   vals["phone"]   = phone
            if website and not existing.website: vals["website"] = website
            if street  and not existing.street:  vals["street"]  = street
            if city    and not existing.city:    vals["city"]    = city
            if zip_code and not existing.zip:    vals["zip"]     = zip_code
            if country_code and not existing.country_id:
                country = self.env["res.country"].search(
                    [("code", "=", country_code.upper())], limit=1
                )
                if country:
                    vals["country_id"] = country.id
                    if state_code and not existing.state_id:
                        state = self.env["res.country.state"].search(
                            [("code", "=", state_code.upper()),
                             ("country_id", "=", country.id)], limit=1
                        )
                        if state:
                            vals["state_id"] = state.id
            if vals:
                existing.write(vals)
            return {"id": existing.id, "display_name": existing.display_name}

        # Create new company partner
        vals = {"name": name, "is_company": True}
        if phone:   vals["phone"]   = phone
        if website: vals["website"] = website
        if street:  vals["street"]  = street
        if city:    vals["city"]    = city
        if zip_code: vals["zip"]    = zip_code
        if country_code:
            country = self.env["res.country"].search(
                [("code", "=", country_code.upper())], limit=1
            )
            if country:
                vals["country_id"] = country.id
                if state_code:
                    state = self.env["res.country.state"].search(
                        [("code", "=", state_code.upper()),
                         ("country_id", "=", country.id)], limit=1
                    )
                    if state:
                        vals["state_id"] = state.id
        partner = self.create(vals)
        return {"id": partner.id, "display_name": partner.display_name}

