"""PHASE 10 — crash-safe bulk email architecture.

Batch → queue row → ONE native mail.mail → send → durable queue state.


* transactional reservation: an item is claimed (queued → sending, send
  attempt + idempotency key) and COMMITTED before any SMTP work — after a
  restart a handed-off item is 'sending', which the cron NEVER picks, so
  it cannot be resent,
* per-item commits after the send write the final state durably,
* one mail.mail per queue item (template rendered into a native outbound
  record) with the canonical threading headers (PHASE 2-3 service) so a
  reply routes back to the SAME opportunity through the normal inbound
  router — no separate threading architecture,
* response tracking: the first meaningful reply on the lead marks the
  item replied with reply_received / reply_received_at /
  response_message_id / response_lead_id,
* bounce suppression: mail.blacklist + partner.is_blacklisted are checked
  before every send (handle_bounce feeds the native counters — PHASE 4-5).
"""
import base64
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)

SUPPRESSED_MSG = 'Suppressed: recipient blacklisted or permanently bounced'

# B-11 — daily send budget for the bulk-queue cron.  0 = unlimited.
# Day boundary = UTC calendar day (the cron stamps naive UTC datetimes).
_PARAM_DAILY_LIMIT = 'crm.bulk_email.daily_limit'


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
    # PHASE 10 — batch analytics
    bounced_count = fields.Integer(compute="_compute_counts", string="Bounced")
    replied_count = fields.Integer(compute="_compute_counts", string="Replied")
    reply_rate = fields.Float(compute="_compute_counts", string="Reply Rate (%)")
    complaints_count = fields.Integer(compute="_compute_counts", string="Complaints")

    @api.depends("queue_ids.state", "queue_ids.complaint_received")
    def _compute_counts(self):
        for batch in self:
            q = batch.queue_ids
            replied = len(q.filtered(lambda r: r.state == "replied"))
            # a replied mail WAS delivered — it counts in sent
            delivered = len(q.filtered(
                lambda r: r.state in ("sent", "replied")))
            batch.total_count = len(q)
            batch.sent_count = delivered
            batch.failed_count = len(q.filtered(lambda r: r.state == "failed"))
            batch.pending_count = len(q.filtered(lambda r: r.state == "queued"))
            batch.bounced_count = len(q.filtered(lambda r: r.state == "bounced"))
            batch.replied_count = replied
            batch.reply_rate = round(
                delivered and (replied * 100.0 / delivered) or 0.0, 1)
            batch.complaints_count = len(q.filtered(lambda r: r.complaint_received))

    def action_cancel(self):
        self.ensure_one()
        self.queue_ids.filtered(
            lambda r: r.state in ("queued", "sending")
        ).write({"state": "cancelled"})
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
            ("sending", "Sending"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("bounced", "Bounced"),
            ("replied", "Replied"),
            ("cancelled", "Cancelled"),
        ],
        default="queued",
        required=True,
        index=True,
        help="queued → sending (transactional reservation) → sent | failed | "
             "bounced; a meaningful reply moves sent items to replied.",
    )
    error_msg = fields.Char("Error")
    mail_id = fields.Many2one("mail.mail", "Mail Record", ondelete="set null")
    # PHASE 10 — idempotency: every send attempt has its own key; the cron
    # never re-picks anything that left 'queued'.
    send_attempt = fields.Integer("Send Attempt", default=0, readonly=True)
    idempotency_key = fields.Char("Idempotency Key", readonly=True, index=True)
    # PHASE 10 — response tracking (written by _mark_replied).
    reply_received = fields.Boolean("Reply Received", readonly=True)
    reply_received_at = fields.Datetime("Reply Received At", readonly=True)
    response_message_id = fields.Char("Response Message-Id", readonly=True)
    response_lead_id = fields.Many2one(
        "crm.lead", "Response Lead", readonly=True, ondelete="set null")
    # PHASE 21 sets this from the provider webhook (complaints).
    complaint_received = fields.Boolean("Complaint Reported", default=False, index=True)

    _sql_constraints = [
        ("idempotency_key_unique", "UNIQUE(idempotency_key)",
         "A bulk send attempt with this idempotency key already exists."),
    ]

    # ── send flow ─────────────────────────────────────────────────────

    def _recipient_suppressed(self):
        """Never email permanently bounced / blacklisted recipients.
        handle_bounce feeds the native counters (PHASE 4-5)."""
        self.ensure_one()
        norm = email_normalize(self.email_to or '')
        if not norm:
            return False
        blacklisted = self.env['mail.blacklist'].sudo().search_count(
            [('email', '=', norm)]) > 0
        partner = self.env['res.partner'].sudo().search(
            [('email_normalized', '=', norm)], limit=1)
        return blacklisted or bool(partner and partner.is_blacklisted)

    def _create_mail(self):
        """ONE native mail.mail for this queue item — rendered from the
        batch template, attached to the lead, canonical threading headers
        (PHASE 2-3 create hook), auto_sent provenance (PHASE 9)."""
        self.ensure_one()
        tpl = self.batch_id.template_id
        # Odoo 18 API: _generate_template(res_ids, render_fields) →
        # {res_id: {field: value}}; core resolves the lead's language.
        rendered = tpl._generate_template(
            [self.lead_id.id],
            ['subject', 'body_html', 'email_from', 'email_cc',
             'attachment_ids', 'report_template_ids'])[self.lead_id.id]
        mail_values = {
            'model': 'crm.lead',
            'res_id': self.lead_id.id,
            'subject': rendered.get('subject') or '',
            'body_html': rendered.get('body_html') or '',
            'email_to': self.email_to,
            'email_cc': rendered.get('email_cc') or False,
            'email_from': rendered.get('email_from') or False,
            'auto_delete': False,
            'idempotency_key': self.idempotency_key,
        }
        # template attachments come back as ids (link them); report
        # attachments as [(name, base64-bytes)] (create real attachments)
        att_ids = list(rendered.get('attachment_ids') or [])
        for name, data in (rendered.get('attachments') or []):
            if data:
                att_ids.append(self.env['ir.attachment'].create({
                    'name': name,
                    'raw': base64.b64decode(data) if isinstance(data, bytes)
                           else data,
                    'res_model': 'mail.mail',
                    'res_id': 0,
                }).id)
        if att_ids:
            mail_values['attachment_ids'] = [(6, 0, att_ids)]
        mail = self.env['mail.mail'].create(mail_values)
        # provenance: bulk template mails are auto-sent, NOT AI-generated
        mail.write({'auto_sent': True})
        return mail

    @api.model
    def _count_daily_sends(self, day_start):
        """Bulk sends that count against the daily quota: items this
        script handed to SMTP today — finalized sends (sent / replied /
        bounced, stamped ``sent_at``) plus in-flight claims made today
        (``state=sending`` by ``write_date``; a stale 'sending' row from a
        previous day never consumes today's budget)."""
        return self.sudo().search_count([
            '|',
            '&', ('state', '=', 'sending'),
                 ('write_date', '>=', day_start),
            '&', ('state', 'in', ('sent', 'replied', 'bounced')),
                 ('sent_at', '>=', day_start),
        ])

    @api.model
    def run_bulk_email_cron(self):
        """Crash-safe sweep: claim + reserve durably, then one record per
        item, then the final state. Items that left 'queued' are never
        re-picked, so a handed-off item cannot be resent after restart.

        B-11 — daily budget: ir.config_parameter
        ``crm.bulk_email.daily_limit`` (0 = unlimited) caps how many items
        this script may hand to SMTP per UTC calendar day.  The guard is
        evaluated at run start against the day's sends (see
        ``_count_daily_sends``) and the claim window is trimmed to the
        remaining budget, so the queue never exceeds the cap across the
        1-minute runs — it simply waits for the next day."""
        now = fields.Datetime.now()
        day_start = datetime.combine(now.date(), datetime.min.time())
        daily_limit = int(self.env['ir.config_parameter'].sudo().get_param(
            _PARAM_DAILY_LIMIT, '0') or 0)
        budget = 50
        if daily_limit > 0:
            already_sent = self._count_daily_sends(day_start)
            if already_sent >= daily_limit:
                _logger.info(
                    'B-11: bulk queue cron skipped — daily limit %s '
                    'reached (%s sent today)', daily_limit, already_sent)
                return 0
            budget = min(50, daily_limit - already_sent)
        due = self.search(
            [("state", "=", "queued"), ("scheduled_at", "<=", now)],
            order="scheduled_at asc",
            limit=budget,
        )
        for item in due:
            # race guard: a parallel worker may have claimed the item
            claim = self.sudo().search(
                [("id", "=", item.id), ("state", "=", "queued")], limit=1)
            if not claim:
                continue
            if claim._recipient_suppressed():
                claim.write({"state": "failed", "error_msg": SUPPRESSED_MSG})
                self.env.cr.commit()
                continue
            # ── transactional reservation (durable BEFORE any SMTP) ──
            claim.write({
                "state": "sending",
                "send_attempt": claim.send_attempt + 1,
                "idempotency_key": "bulk-%d-%d-%d" % (
                    claim.batch_id.id, claim.lead_id.id, claim.send_attempt + 1),
            })
            self.env.cr.commit()
            try:
                mail = claim._create_mail()
                claim.write({"mail_id": mail.id, "subject": mail.subject or ""})
                self.env.cr.commit()
                mail.send(raise_exception=False)
                mail.invalidate_recordset()
                if mail.state == "exception":
                    if mail.failure_type == "bounce":
                        claim.write({
                            "state": "bounced",
                            "sent_at": fields.Datetime.now(),
                            "error_msg": mail.failure_reason or "Permanent bounce",
                        })
                    else:
                        claim.write({
                            "state": "failed",
                            "error_msg": mail.failure_reason or "Send failed",
                        })
                else:
                    claim.write({
                        "state": "sent",
                        "sent_at": fields.Datetime.now(),
                    })
                    # PHASE 14 — a bulk outreach is an initiated outbound
                    # touch: stamp the reply-status fields (mail.mail sends
                    # do not pass through _message_post_after_hook) and run
                    # the response discipline (schedule the stage's next
                    # follow-up).
                    lead = claim.lead_id.sudo()
                    lead.write({'last_outbound_at': fields.Datetime.now(),
                                'last_outreach_at': fields.Datetime.now()})
                    lead._on_sales_response()
                self.env.cr.commit()
            except Exception as exc:
                claim.write({"state": "failed", "error_msg": str(exc)[:250]})
                self.env.cr.commit()

        for batch in due.mapped("batch_id"):
            q = batch.queue_ids
            all_done = all(
                r.state in ("sent", "failed", "cancelled", "bounced", "replied")
                for r in q
            )
            any_sent = any(r.state in ("sent", "replied") for r in q)
            if all_done:
                batch.state = "done"
            elif any_sent:
                batch.state = "in_progress"

    # ── response tracking ─────────────────────────────────────────────

    @api.model
    def _mark_replied(self, lead_id, response_message_id=False):
        """A meaningful reply landed on a lead with an open bulk item:
        record it on the item and move it to 'replied'. First reply wins
        the timestamps; later replies update the state but not the data."""
        items = self.sudo().search([
            ("lead_id", "=", lead_id),
            ("state", "in", ("queued", "sending", "sent")),
        ])
        now = fields.Datetime.now()
        for item in items:
            vals = {"state": "replied"}
            if not item.reply_received:
                vals.update({
                    "reply_received": True,
                    "reply_received_at": now,
                    "response_message_id": response_message_id or False,
                    "response_lead_id": lead_id,
                })
            item.write(vals)
        return bool(items)


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
