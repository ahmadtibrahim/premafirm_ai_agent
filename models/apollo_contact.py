from odoo import api, fields, models
from odoo.exceptions import UserError


class PremafirmApolloContact(models.Model):
    _name = "premafirm.apollo.contact"
    _description = "Apollo Prospect Contact"
    _rec_name = "name"
    _order = "is_primary desc, name"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, ondelete="cascade", index=True)
    apollo_person_id = fields.Char("Apollo Person ID", index=True)

    first_name = fields.Char("First Name")
    last_name = fields.Char("Last Name")
    name = fields.Char("Full Name", compute="_compute_name", store=True)
    email = fields.Char("Email")
    phone = fields.Char("Phone")
    title = fields.Char("Job Title")
    company_name = fields.Char("Company")
    linkedin_url = fields.Char("LinkedIn URL")
    city = fields.Char("City")
    state_name = fields.Char("State/Province")
    country = fields.Char("Country")
    industry = fields.Char("Industry")

    is_primary = fields.Boolean("Primary Contact", default=False)
    enriched = fields.Boolean("Enriched", default=False)
    source = fields.Selection([("apollo", "Apollo"), ("manual", "Manual")], default="apollo", string="Source")
    notes = fields.Text("Notes")

    outreach_stage = fields.Selection(
        [
            ("new", "New"),
            ("contacted", "Contacted"),
            ("followup_1", "Follow-up 1"),
            ("followup_2", "Follow-up 2"),
            ("no_response", "No Response"),
            ("replied", "Replied"),
            ("converted", "Converted"),
        ],
        string="Stage",
        default="new",
        tracking=True,
    )
    outreach_ids = fields.One2many("premafirm.crm.outreach", "contact_id", string="Outreach History")

    @api.depends("first_name", "last_name")
    def _compute_name(self):
        for rec in self:
            parts = [rec.first_name or "", rec.last_name or ""]
            rec.name = " ".join(p for p in parts if p).strip() or "Unknown"

    def action_set_primary(self):
        self.ensure_one()
        # Clear primary on all siblings
        self.lead_id.apollo_contact_ids.write({"is_primary": False})
        self.is_primary = True
        self.lead_id.primary_contact_id = self.id

    def action_enrich(self):
        """Enrich this contact via Apollo. USES CREDITS — user-initiated only."""
        self.ensure_one()
        from ..services.apollo_service import ApolloService

        service = ApolloService(self.env)
        try:
            result = service.enrich_person(
                email=self.email,
                first_name=self.first_name,
                last_name=self.last_name,
                organization_name=self.company_name,
            )
        except ValueError as e:
            raise UserError(str(e))

        person = result.get("person") or {}
        if not person:
            raise UserError("Apollo returned no match for this contact.")

        org = person.get("organization") or {}
        self.write({
            "phone": person.get("sanitized_phone") or self.phone,
            "email": person.get("email") or self.email,
            "linkedin_url": person.get("linkedin_url") or self.linkedin_url,
            "city": person.get("city") or self.city,
            "state_name": person.get("state") or self.state_name,
            "country": person.get("country") or self.country,
            "industry": (org.get("industry") or self.industry),
            "enriched": True,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Enriched",
                "message": f"{self.name} enriched successfully.",
                "type": "success",
                "sticky": False,
            },
        }
