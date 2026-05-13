from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class PremafirmCrmBulkEmailBatch(models.Model):
    _name = "premafirm.crm.bulk.email.batch"
    _description = "Bulk Email Batch"
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(
        "Batch Name",
        required=True,
        default=lambda self: "Batch " + fields.Datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    template_id = fields.Many2one(
        "mail.template",
        "Email Template",
        required=True,
        domain=[("model", "=", "crm.lead")],
        ondelete="restrict",
    )
    scheduled_at = fields.Datetime("Starts At", required=True)
    delay_seconds = fields.Integer("Delay Between Emails (sec)", default=10, required=True)
    state = fields.Selection(
        [
            ("scheduled", "Scheduled"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        default="scheduled",
        required=True,
        tracking=True,
    )
    queue_ids = fields.One2many("premafirm.crm.bulk.email.queue", "batch_id", "Email Queue")
    total_count = fields.Integer(compute="_compute_counts", string="Total")
    sent_count = fields.Integer(compute="_compute_counts", string="Sent")
    failed_count = fields.Integer(compute="_compute_counts", string="Failed")
    pending_count = fields.Integer(compute="_compute_counts", string="Queued")

    @api.depends("queue_ids.state")
    def _compute_counts(self):
        for batch in self:
            q = batch.queue_ids
            batch.total_count = len(q)
            batch.sent_count = len(q.filtered(lambda r: r.state == "sent"))
            batch.failed_count = len(q.filtered(lambda r: r.state == "failed"))
            batch.pending_count = len(q.filtered(lambda r: r.state == "queued"))

    def action_cancel(self):
        self.ensure_one()
        self.queue_ids.filtered(lambda r: r.state == "queued").write({"state": "cancelled"})
        self.state = "cancelled"


class PremafirmCrmBulkEmailQueue(models.Model):
    _name = "premafirm.crm.bulk.email.queue"
    _description = "Bulk Email Queue Item"
    _order = "scheduled_at asc"

    batch_id = fields.Many2one(
        "premafirm.crm.bulk.email.batch", "Batch", required=True, ondelete="cascade", index=True
    )
    lead_id = fields.Many2one("crm.lead", "Lead", required=True, ondelete="cascade", index=True)
    email_to = fields.Char("Email To", required=True)
    subject = fields.Char("Subject")
    scheduled_at = fields.Datetime("Send At", required=True, index=True)
    sent_at = fields.Datetime("Sent At")
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("bounced", "Bounced"),
            ("cancelled", "Cancelled"),
        ],
        default="queued",
        required=True,
        index=True,
    )
    error_msg = fields.Char("Error")
    mail_id = fields.Many2one("mail.mail", "Mail Record", ondelete="set null")

    @api.model
    def run_bulk_email_cron(self):
        now = fields.Datetime.now()
        due = self.search(
            [("state", "=", "queued"), ("scheduled_at", "<=", now)],
            order="scheduled_at asc",
        )
        for item in due:
            try:
                template = item.batch_id.template_id
                mail_id = template.send_mail(
                    item.lead_id.id,
                    force_send=True,
                    raise_exception=False,
                    email_values={"email_to": item.email_to, "auto_delete": False},
                )
                mail = self.env["mail.mail"].sudo().browse(mail_id)
                new_state = "failed" if mail.state == "exception" else "sent"
                item.write(
                    {
                        "state": new_state,
                        "sent_at": fields.Datetime.now(),
                        "mail_id": mail.id,
                        "subject": mail.subject or "",
                        "error_msg": mail.failure_reason if new_state == "failed" else False,
                    }
                )
            except Exception as exc:
                item.write({"state": "failed", "error_msg": str(exc)[:250]})

        for batch in due.mapped("batch_id"):
            all_done = all(
                q.state in ("sent", "failed", "cancelled", "bounced") for q in batch.queue_ids
            )
            any_sent = any(q.state == "sent" for q in batch.queue_ids)
            if all_done:
                batch.state = "done"
            elif any_sent:
                batch.state = "in_progress"


class PremafirmCrmBulkEmailWizard(models.TransientModel):
    _name = "premafirm.crm.bulk.email.wizard"
    _description = "Bulk Email Wizard"

    lead_ids = fields.Many2many("crm.lead", string="Selected Leads")
    lead_count = fields.Integer(compute="_compute_lead_count", string="Leads Selected")
    template_id = fields.Many2one(
        "mail.template",
        "Email Template",
        domain=[("model", "=", "crm.lead")],
    )
    send_mode = fields.Selection(
        [("now", "Send Now"), ("scheduled", "Schedule for Later")],
        default="now",
        required=True,
        string="When to Send",
    )
    scheduled_at = fields.Datetime("Start Sending At")
    delay_seconds = fields.Integer("Delay Between Emails (sec)", default=10, required=True)

    @api.depends("lead_ids")
    def _compute_lead_count(self):
        for w in self:
            w.lead_count = len(w.lead_ids)

    @api.model
    def action_open_wizard(self, lead_ids):
        wizard = self.create({"lead_ids": [(6, 0, lead_ids)]})
        return {
            "type": "ir.actions.act_window",
            "name": "Schedule Bulk Email",
            "res_model": "premafirm.crm.bulk.email.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_launch(self):
        self.ensure_one()
        if not self.template_id:
            raise UserError("Please select an email template.")
        if self.send_mode == "scheduled" and not self.scheduled_at:
            raise UserError("Please set a start date/time for the scheduled send.")
        if self.delay_seconds < 0:
            raise UserError("Delay cannot be negative.")

        start = self.scheduled_at if self.send_mode == "scheduled" else fields.Datetime.now()

        batch = self.env["premafirm.crm.bulk.email.batch"].create(
            {
                "template_id": self.template_id.id,
                "scheduled_at": start,
                "delay_seconds": self.delay_seconds,
                "state": "scheduled",
            }
        )

        queue_vals = []
        idx = 0
        for lead in self.lead_ids:
            email = lead.email_from

            if not email:
                continue

            queue_vals.append(
                {
                    "batch_id": batch.id,
                    "lead_id": lead.id,
                    "email_to": email,
                    "scheduled_at": start + timedelta(seconds=idx * self.delay_seconds),
                    "state": "queued",
                }
            )
            idx += 1

        if not queue_vals:
            batch.unlink()
            raise UserError(
                "None of the selected leads have an email address. Please add emails first."
            )

        self.env["premafirm.crm.bulk.email.queue"].create(queue_vals)

        return {
            "type": "ir.actions.act_window",
            "name": "Bulk Email Batch",
            "res_model": "premafirm.crm.bulk.email.batch",
            "res_id": batch.id,
            "view_mode": "form",
            "target": "current",
        }
