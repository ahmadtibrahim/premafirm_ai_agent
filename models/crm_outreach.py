from odoo import api, fields, models


class PremafirmCrmOutreach(models.Model):
    _name = "premafirm.crm.outreach"
    _description = "CRM Outreach Record"
    _order = "sent_at desc, id desc"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, ondelete="cascade", index=True)
    contact_id = fields.Many2one("premafirm.snov.contact", string="Contact", ondelete="set null")
    contact_email = fields.Char(related="contact_id.email", string="Contact Email", store=True)

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
        required=True,
        default="new",
        tracking=True,
    )

    email_subject = fields.Char("Email Subject")
    email_body = fields.Text("Email Body")
    sent_at = fields.Datetime("Sent At")
    reply_received = fields.Boolean("Reply Received", default=False)
    reply_received_at = fields.Datetime("Reply Received At")
    next_followup_date = fields.Date("Next Follow-up Date")
    notes = fields.Text("Notes")

    @api.model
    def _add_business_days(self, start_date, days):
        from datetime import timedelta
        d = start_date
        added = 0
        while added < days:
            d += timedelta(days=1)
            if d.weekday() < 5:
                added += 1
        return d

    @api.model
    def run_outreach_followup(self):
        """Daily cron: advance overdue outreach stages."""
        today = fields.Date.today()
        params = self.env["ir.config_parameter"].sudo()
        days_1 = int(params.get_param("outreach.followup_days_1", 3))
        days_2 = int(params.get_param("outreach.followup_days_2", 7))

        due = self.search([
            ("next_followup_date", "<=", today),
            ("reply_received", "=", False),
            ("outreach_stage", "in", ["contacted", "followup_1", "followup_2"]),
        ])

        for rec in due:
            if rec.outreach_stage == "contacted":
                rec.write({
                    "outreach_stage": "followup_1",
                    "next_followup_date": self._add_business_days(today, days_1),
                })
                if rec.contact_id:
                    rec.contact_id.outreach_stage = "followup_1"

            elif rec.outreach_stage == "followup_1":
                rec.write({
                    "outreach_stage": "followup_2",
                    "next_followup_date": self._add_business_days(today, days_2),
                })
                if rec.contact_id:
                    rec.contact_id.outreach_stage = "followup_2"

            elif rec.outreach_stage == "followup_2":
                rec.write({"outreach_stage": "no_response"})
                if rec.contact_id:
                    rec.contact_id.outreach_stage = "no_response"
