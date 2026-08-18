"""PHASE 8 — reply-status fields on crm.lead + "CRM: Replied" gate.

Field set (spec): last_inbound_at, last_meaningful_reply_at, last_outbound_at,
last_outreach_at, reply_received, needs_reply, waiting_on_customer,
next_followup_at, last_inbound_classification.

Who writes what:

* INBOUND — stamped at ROUTE time by the classifier (inbound_routing.py):
  new-inquiry leads carry their classification in the create values, matched
  normal-reply leads are stamped before the post. The "CRM: Replied"
  automation (trigger ``on_message_received``) is gated to
  ``last_inbound_classification == 'normal_reply'`` via ``filter_pre_domain``
  — the ONLY domain the message-trigger path evaluates (base_automation
  monkey-patches message_post and calls ``_filter_pre``; ``filter_domain``
  is never consulted there). Bounces, OOO/auto-replies and new inquiries
  therefore can never stamp the Replied stage.
* OUTBOUND — stamped in ``_message_post_after_hook``: an email authored by an
  internal user is outbound; with no parent/references it is outreach
  (initiated by us), otherwise it is a reply to an existing thread.
* CUSTOMER-authored chatter comments (portal) count as meaningful engagement.
* The human-confirmed queue attach (action_thread_to_lead) stamps a
  meaningful reply ONLY when the queued mail was classified
  ``unmatched_reply`` — bounce/OOO attaches never count as engagement.

``next_followup_at`` is managed by the Follow-Up Service (PHASE 15) — it is
declared here, not yet computed.

Existing rows are backfilled from the legacy studio field
``x_reply_received_at`` in the 18.0.6.10.0 migration.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Mirrors the classification selection of premafirm.inbound.queue (the
# classifier's 10 classes + the 2 reviewer-only entries).
_INBOUND_CLASSES = [
    ('normal_reply', 'Normal Reply'),
    ('new_inquiry', 'New Inquiry'),
    ('unmatched_reply', 'Unmatched Reply'),
    ('bounce', 'Bounce'),
    ('delivery_failure', 'Delivery Failure'),
    ('out_of_office', 'Out of Office'),
    ('auto_reply', 'Auto-Reply'),
    ('mailing_list', 'Mailing List'),
    ('system_message', 'System Message'),
    ('spam_or_noise', 'Spam / Noise'),
    ('internal', 'Internal Noise'),
    ('other', 'Other'),
]


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # ── PHASE 8 reply-status fields ──────────────────────────────
    last_inbound_at = fields.Datetime('Last Inbound Message', index=True)
    last_meaningful_reply_at = fields.Datetime(
        'Last Meaningful Reply', index=True,
        help='Last genuine customer reply. Bounces, OOO and auto-replies '
             'never set this.')
    last_outbound_at = fields.Datetime('Last Outbound Message', index=True)
    last_outreach_at = fields.Datetime(
        'Last Outreach', index=True,
        help='Last outbound email we initiated (not a reply).')
    last_inbound_classification = fields.Selection(
        _INBOUND_CLASSES, 'Last Inbound Classification', index=True)
    next_followup_at = fields.Datetime(
        'Next Follow-up', index=True,
        help='Next scheduled follow-up touch. Written by the Follow-Up '
             'Service (PHASE 15).')

    # Computed flags — derived from the timestamps, stored for filtering.
    reply_received = fields.Boolean(
        'Reply Received', compute='_compute_reply_flags', store=True)
    needs_reply = fields.Boolean(
        'Needs Reply', compute='_compute_reply_flags', store=True,
        help='We received inbound mail (inquiry or reply) and have not '
             'answered yet.')
    waiting_on_customer = fields.Boolean(
        'Waiting on Customer', compute='_compute_reply_flags', store=True,
        help='Our last touch was outbound — we are waiting on them.')

    @api.depends('last_meaningful_reply_at', 'last_inbound_at',
                 'last_outbound_at')
    def _compute_reply_flags(self):
        for lead in self:
            last_out = lead.last_outbound_at
            last_in = lead.last_inbound_at
            lead.reply_received = bool(lead.last_meaningful_reply_at)
            lead.waiting_on_customer = (
                bool(last_out) and (not last_in or last_out >= last_in))
            lead.needs_reply = (
                bool(last_in) and (not last_out or last_in > last_out))

    def _message_post_after_hook(self, message, msg_values):
        """Track outbound/outreach timestamps on every path (composer,
        mail.mail, bulk, follow-up, AI service).

        Outbound = an INTERNAL user's partner authored the message
        (mail.mail sends post as ``email_outgoing`` — PHASE 17 fix;
        customer partners have no user link, so ``user_ids`` is the
        airtight discriminator, not ``partner_share``).

        PHASE 29 — the native Odoo 18 chatter/composer posts customer
        emails as message_type ``comment`` (mail.compose.message posts
        its wizard selection, default ``comment``; the email goes out
        through the notification queue).  A comment authored by an
        internal user with a From address and at least one EXTERNAL
        recipient is a customer email, not an internal note — it must
        stamp outbound exactly like an email send, or Needs Reply /
        follow-up discipline dies on the primary UI path.  Internal
        notes (no external recipient, no From address) stay unstamped."""
        res = super()._message_post_after_hook(message, msg_values)
        author = message.author_id
        now = fields.Datetime.now()
        internal = author and any(
            not u.share for u in author.user_ids)
        external_recipients = message.partner_ids.filtered(
            lambda p: not p.user_ids.filtered(lambda u: not u.share))
        is_email_like = (
            message.message_type in ('email', 'email_outgoing')
            or (message.message_type == 'comment'
                and message.email_from and external_recipients))
        if is_email_like and internal:
            # An internal user's partner authored the email → outbound.
            # Outreach = we initiated a new email thread.  The composer's
            # INTENT is captured by the crm.lead.message_post override
            # (premafirm_post_intent context) BEFORE message_post recomputes
            # parent_id: under flat threading a fresh composer email and a
            # reply both end up parented to the thread's first message, so
            # the created message's parent cannot distinguish outreach from
            # an answer.  Paths that do not route through the override fall
            # back to the post values' own threading headers.
            intent = self.env.context.get('premafirm_post_intent')
            if intent is None:
                initiated_by_us = not (msg_values.get('references')
                                       or msg_values.get('in_reply_to'))
            else:
                initiated_by_us = not intent
            vals = {'last_outbound_at': now}
            if initiated_by_us:
                vals['last_outreach_at'] = now
            self.write(vals)
            # PHASE 14 — response discipline: complete Respond to Customer
            # activities, schedule the stage's next follow-up.
            self._on_sales_response()
        elif (message.message_type == 'comment'
                and author and author.partner_share):
            # Customer-authored chatter (portal comment) = engagement.
            self.write({
                'last_inbound_at': now,
                'last_meaningful_reply_at': now,
                'last_inbound_classification': 'normal_reply',
            })
            # PHASE 10 — portal reply answers any open bulk item too.
            self.env['premafirm.crm.bulk.email.queue']._mark_replied(
                self.id, response_message_id=message.message_id or False)
            # PHASE 14 — reply discipline (see _on_meaningful_reply).
            self._on_meaningful_reply()
        return res
