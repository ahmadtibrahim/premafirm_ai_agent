from odoo import api, fields, models
from odoo.exceptions import UserError


class PremafirmSnovContact(models.Model):
    _name = "premafirm.snov.contact"
    _description = "Snov.io Prospect Contact"
    _rec_name = "name"
    _order = "is_primary desc, name"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, ondelete="cascade", index=True)
    snov_person_id = fields.Char("Snov Person ID", index=True)

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
    email_verified = fields.Selection(
        [("valid", "Valid"), ("invalid", "Invalid"), ("unknown", "Unknown"), ("catch_all", "Catch-All")],
        string="Email Status",
    )
    source = fields.Selection([("snov", "Snov.io"), ("manual", "Manual")], default="snov", string="Source")
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
        self.lead_id.snov_contact_ids.write({"is_primary": False})
        self.is_primary = True
        self.lead_id.primary_contact_id = self.id

    def action_verify_email(self):
        """Verify this contact's email via Snov.io. USES CREDITS — user-initiated only."""
        self.ensure_one()
        if not self.email:
            raise UserError("No email address to verify.")
        from ..services.snov_service import SnovService
        service = SnovService(self.env)
        try:
            result = service.verify_email(self.email)
        except ValueError as e:
            raise UserError(str(e))
        status = (result.get("result") or "unknown").lower().replace("-", "_")
        if status == "catch_all":
            status = "catch_all"
        elif status not in ("valid", "invalid", "unknown", "catch_all"):
            status = "unknown"
        self.email_verified = status
        label = {"valid": "Valid", "invalid": "Invalid", "unknown": "Unknown", "catch_all": "Catch-All"}.get(status, status)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Email Verified",
                "message": f"{self.email} — {label}",
                "type": "success" if status == "valid" else "warning",
                "sticky": False,
            },
        }

    def action_enrich(self):
        """Find email via Snov.io person lookup. USES CREDITS — user-initiated only."""
        self.ensure_one()
        if not self.first_name or not self.last_name:
            raise UserError("First and last name are required to find an email.")
        domain = ""
        if self.lead_id and self.lead_id.partner_id and self.lead_id.partner_id.website:
            website = self.lead_id.partner_id.website or ""
            domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
        if not domain:
            raise UserError("Cannot find domain. Set the company website on the linked CRM lead's contact.")
        from ..services.snov_service import SnovService
        service = SnovService(self.env)
        try:
            result = service.find_email_by_person(self.first_name, self.last_name, domain)
        except ValueError as e:
            raise UserError(str(e))
        emails = result.get("emails") or []
        if not emails:
            raise UserError("Snov.io found no email for this person. The result may arrive later — try again in a moment.")
        best = emails[0]
        self.write({
            "email": best.get("email") or self.email,
            "enriched": True,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Email Found",
                "message": f"Found: {best.get('email')}",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def snov_domain_search(self, domain, positions=None, limit=10):
        """Cross-module API: any installed module can call Snov.io via env."""
        from ..services.snov_service import SnovService
        return SnovService(self.env).find_emails_by_domain(
            domain=domain, positions=positions, limit=limit
        )
