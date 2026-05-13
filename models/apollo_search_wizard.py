from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.apollo_service import CARRIER_PROSPECT_TITLES

_CARRIER_TITLES_DEFAULT = ", ".join(CARRIER_PROSPECT_TITLES[:8])


class ApolloSearchWizard(models.TransientModel):
    _name = "apollo.search.wizard"
    _description = "Apollo Contact Search"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True)
    company_name = fields.Char("Company Name")
    city = fields.Char("City")
    country = fields.Char("Country")
    job_titles = fields.Char(
        "Job Titles",
        default=_CARRIER_TITLES_DEFAULT,
        help="Comma-separated. Pre-filled with titles that typically buy freight/logistics services.",
    )
    keywords = fields.Char("Keywords")
    suggested_titles = fields.Text(
        "All Suggested Titles",
        compute="_compute_suggested_titles",
    )

    state = fields.Selection([("search", "Search"), ("results", "Results")], default="search")
    result_ids = fields.One2many("apollo.search.wizard.result", "wizard_id", string="Results")
    result_count = fields.Integer(compute="_compute_result_count")
    error_msg = fields.Text("Error")

    @api.depends()
    def _compute_suggested_titles(self):
        all_titles = ", ".join(CARRIER_PROSPECT_TITLES)
        for rec in self:
            rec.suggested_titles = all_titles

    @api.depends("result_ids")
    def _compute_result_count(self):
        for rec in self:
            rec.result_count = len(rec.result_ids)

    def action_use_all_titles(self):
        self.job_titles = ", ".join(CARRIER_PROSPECT_TITLES)
        return self._reopen()

    def action_search(self):
        self.ensure_one()
        from ..services.apollo_service import ApolloService

        service = ApolloService(self.env)
        filters = {}
        if self.company_name:
            filters["company_name"] = self.company_name
        if self.city:
            filters["city"] = self.city
        if self.country:
            filters["country"] = self.country
        if self.job_titles:
            filters["job_titles"] = self.job_titles
        if self.keywords:
            filters["keywords"] = self.keywords

        try:
            data = service.search_people(filters, per_page=25)
        except ValueError as e:
            self.error_msg = str(e)
            self.state = "results"
            return self._reopen()

        self.result_ids.unlink()
        people = data.get("people") or []
        for person in people[:25]:
            org = person.get("organization") or {}
            self.env["apollo.search.wizard.result"].create({
                "wizard_id": self.id,
                "apollo_person_id": person.get("id") or "",
                "first_name": person.get("first_name") or "",
                "last_name": person.get("last_name") or "",
                "email": person.get("email") or "",
                "phone": person.get("sanitized_phone") or "",
                "title": person.get("title") or "",
                "company_name": org.get("name") or person.get("organization_name") or "",
                "linkedin_url": person.get("linkedin_url") or "",
                "city": person.get("city") or "",
                "country": person.get("country") or "",
            })

        self.state = "results"
        self.error_msg = False
        return self._reopen()

    def action_import_selected(self):
        self.ensure_one()
        selected = self.result_ids.filtered("selected")
        if not selected:
            raise UserError("Select at least one contact to import.")

        created = 0
        for result in selected:
            existing = self.env["premafirm.apollo.contact"].search([
                ("lead_id", "=", self.lead_id.id),
                ("apollo_person_id", "=", result.apollo_person_id),
            ], limit=1)
            if existing:
                continue
            self.env["premafirm.apollo.contact"].create({
                "lead_id": self.lead_id.id,
                "apollo_person_id": result.apollo_person_id,
                "first_name": result.first_name,
                "last_name": result.last_name,
                "email": result.email,
                "phone": result.phone,
                "title": result.title,
                "company_name": result.company_name,
                "linkedin_url": result.linkedin_url,
                "city": result.city,
                "source": "apollo",
            })
            created += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Contacts Imported",
                "message": f"{created} contact(s) added to the lead.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

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


class ApolloSearchWizardResult(models.TransientModel):
    _name = "apollo.search.wizard.result"
    _description = "Apollo Search Result"
    _rec_name = "display_name_computed"

    wizard_id = fields.Many2one("apollo.search.wizard", ondelete="cascade")
    selected = fields.Boolean("Import", default=False)
    apollo_person_id = fields.Char("Apollo ID")
    first_name = fields.Char("First Name")
    last_name = fields.Char("Last Name")
    display_name_computed = fields.Char(compute="_compute_display_name_computed", store=True)
    email = fields.Char("Email")
    phone = fields.Char("Phone")
    title = fields.Char("Job Title")
    company_name = fields.Char("Company")
    linkedin_url = fields.Char("LinkedIn")
    city = fields.Char("City")
    country = fields.Char("Country")

    @api.depends("first_name", "last_name")
    def _compute_display_name_computed(self):
        for rec in self:
            parts = [rec.first_name or "", rec.last_name or ""]
            rec.display_name_computed = " ".join(p for p in parts if p).strip() or "Unknown"
