from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.snov_service import CARRIER_PROSPECT_TITLES

_CARRIER_TITLES_DEFAULT = ", ".join(CARRIER_PROSPECT_TITLES[:8])


class SnovSearchWizard(models.TransientModel):
    _name = "snov.search.wizard"
    _description = "Snov.io Prospect Search"

    # Optional — if set, results are added to this lead's Outreach tab.
    # If blank, results create new CRM leads.
    lead_id = fields.Many2one("crm.lead", string="Add to Lead (optional)",
                               help="Leave blank to create new CRM leads for each selected contact.")

    domain = fields.Char(
        "Company Domain",
        help="e.g. loblaw.com — Snov.io finds all known contacts at this domain.",
    )
    positions = fields.Char(
        "Job Title Filter",
        default=_CARRIER_TITLES_DEFAULT,
        help="Comma-separated job titles. Leave blank to get all contacts at the domain.",
    )
    limit = fields.Integer("Max Results", default=10)

    suggested_titles = fields.Text("All Suggested Titles", compute="_compute_suggested_titles")
    state = fields.Selection([("search", "Search"), ("results", "Results")], default="search")
    result_ids = fields.One2many("snov.search.wizard.result", "wizard_id", string="Results")
    result_count = fields.Integer(compute="_compute_result_count")
    selected_count = fields.Integer(compute="_compute_result_count")
    error_msg = fields.Text("Error")

    @api.depends()
    def _compute_suggested_titles(self):
        all_titles = ", ".join(CARRIER_PROSPECT_TITLES)
        for rec in self:
            rec.suggested_titles = all_titles

    @api.depends("result_ids", "result_ids.selected")
    def _compute_result_count(self):
        for rec in self:
            rec.result_count = len(rec.result_ids)
            rec.selected_count = len(rec.result_ids.filtered("selected"))

    def action_use_all_titles(self):
        self.positions = ", ".join(CARRIER_PROSPECT_TITLES)
        return self._reopen()

    def action_select_all(self):
        self.result_ids.write({"selected": True})
        return self._reopen()

    def action_deselect_all(self):
        self.result_ids.write({"selected": False})
        return self._reopen()

    def action_search(self):
        self.ensure_one()
        if not self.domain:
            raise UserError("Enter a company domain to search (e.g. loblaw.com).")
        from ..services.snov_service import SnovService
        service = SnovService(self.env)
        try:
            data = service.find_emails_by_domain(
                domain=self.domain,
                positions=self.positions or None,
                limit=min(self.limit or 10, 100),
            )
        except ValueError as e:
            self.error_msg = str(e)
            self.state = "results"
            return self._reopen()

        self.result_ids.unlink()
        emails = data.get("emails") or []
        if not emails:
            self.error_msg = (
                "No contacts found for this domain"
                + (" with the given title filters." if self.positions else ".")
                + " Try removing the title filter or check the domain spelling."
            )
            self.state = "results"
            return self._reopen()

        for item in emails:
            confidence_raw = item.get("confidence") or item.get("confidenceScore") or 0
            if isinstance(confidence_raw, str):
                confidence_raw = {"high": 90, "medium": 60, "low": 30}.get(
                    confidence_raw.lower(), 0
                )
            self.env["snov.search.wizard.result"].create({
                "wizard_id": self.id,
                "snov_person_id": str(item.get("id") or ""),
                "first_name": item.get("firstName") or "",
                "last_name": item.get("lastName") or "",
                "email": item.get("email") or "",
                "title": item.get("currentTitle") or item.get("position") or "",
                "company_name": item.get("currentCompany") or "",
                "linkedin_url": item.get("linkedInUrl") or "",
                "country": item.get("country") or "",
                "city": item.get("city") or "",
                "confidence": int(confidence_raw),
                "selected": True,
            })

        self.state = "results"
        self.error_msg = False
        return self._reopen()

    # ── Import to existing lead ────────────────────────────────────────────────

    def action_import_to_lead(self):
        """Add selected contacts to an existing CRM lead's Outreach tab."""
        self.ensure_one()
        if not self.lead_id:
            raise UserError("Select a lead to add contacts to.")
        selected = self.result_ids.filtered("selected")
        if not selected:
            raise UserError("Select at least one contact.")
        created = 0
        for result in selected:
            existing = self.env["premafirm.snov.contact"].search([
                ("lead_id", "=", self.lead_id.id),
                ("email", "=", result.email),
            ], limit=1)
            if existing:
                continue
            self.env["premafirm.snov.contact"].create(
                self._snov_contact_vals(result, self.lead_id.id)
            )
            created += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Contacts Added",
                "message": f"{created} contact(s) added to the lead's Outreach tab.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    # ── Create new CRM Leads ───────────────────────────────────────────────────

    def action_create_leads(self):
        """Create a new CRM lead for each selected contact."""
        self.ensure_one()
        selected = self.result_ids.filtered("selected")
        if not selected:
            raise UserError("Select at least one contact.")

        created_leads = self.env["crm.lead"]
        for result in selected:
            lead = self._create_lead_from_result(result)
            created_leads |= lead

        count = len(created_leads)
        if count == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "crm.lead",
                "res_id": created_leads.id,
                "view_mode": "form",
                "target": "current",
            }
        return {
            "type": "ir.actions.act_window",
            "name": f"{count} New Leads",
            "res_model": "crm.lead",
            "view_mode": "list,form",
            "domain": [("id", "in", created_leads.ids)],
            "target": "current",
        }

    def _create_lead_from_result(self, result):
        """Create res.partner (company + person) and CRM lead from a search result."""
        domain = self.domain or ""
        company_name = result.company_name or domain

        # Find or create company partner
        company_partner = False
        if company_name:
            company_partner = self.env["res.partner"].search(
                [("name", "=ilike", company_name), ("is_company", "=", True)], limit=1
            )
            if not company_partner:
                company_partner = self.env["res.partner"].create({
                    "name": company_name,
                    "is_company": True,
                    "website": f"https://www.{domain}" if domain else False,
                    "country_id": self._country_id(result.country),
                })

        # Find or create person partner
        person_partner = False
        full_name = f"{result.first_name} {result.last_name}".strip()
        if result.email:
            person_partner = self.env["res.partner"].search(
                [("email", "=ilike", result.email)], limit=1
            )
        if not person_partner and full_name:
            person_partner = self.env["res.partner"].create({
                "name": full_name or company_name,
                "email": result.email,
                "function": result.title,
                "parent_id": company_partner.id if company_partner else False,
                "country_id": self._country_id(result.country),
                "city": result.city or False,
                "website": result.linkedin_url or False,
            })

        lead_name = f"{company_name} — {result.title}" if result.title else company_name
        lead = self.env["crm.lead"].create({
            "name": lead_name,
            "partner_id": person_partner.id if person_partner else (
                company_partner.id if company_partner else False
            ),
            "partner_name": company_name,
            "contact_name": full_name,
            "email_from": result.email,
            "outreach_stage": "new",
        })

        self.env["premafirm.snov.contact"].create(
            self._snov_contact_vals(result, lead.id, is_primary=True)
        )
        return lead

    def _snov_contact_vals(self, result, lead_id, is_primary=False):
        return {
            "lead_id": lead_id,
            "snov_person_id": result.snov_person_id,
            "first_name": result.first_name,
            "last_name": result.last_name,
            "email": result.email,
            "title": result.title,
            "company_name": result.company_name,
            "linkedin_url": result.linkedin_url,
            "country": result.country,
            "city": result.city,
            "is_primary": is_primary,
            "source": "snov",
        }

    def _country_id(self, country_name):
        if not country_name:
            return False
        country = self.env["res.country"].search(
            [("name", "=ilike", country_name)], limit=1
        )
        return country.id if country else False

    def action_back(self):
        self.state = "search"
        return self._reopen()

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class SnovSearchWizardResult(models.TransientModel):
    _name = "snov.search.wizard.result"
    _description = "Snov.io Search Result"
    _rec_name = "display_name_computed"

    wizard_id = fields.Many2one("snov.search.wizard", ondelete="cascade")
    selected = fields.Boolean("Import", default=True)
    snov_person_id = fields.Char("Snov ID")
    first_name = fields.Char("First Name")
    last_name = fields.Char("Last Name")
    display_name_computed = fields.Char(compute="_compute_display_name_computed", store=True)
    email = fields.Char("Email")
    title = fields.Char("Job Title")
    company_name = fields.Char("Company")
    linkedin_url = fields.Char("LinkedIn")
    country = fields.Char("Country")
    city = fields.Char("City")
    confidence = fields.Integer("Confidence %")

    @api.depends("first_name", "last_name")
    def _compute_display_name_computed(self):
        for rec in self:
            parts = [rec.first_name or "", rec.last_name or ""]
            rec.display_name_computed = " ".join(p for p in parts if p).strip() or "Unknown"
