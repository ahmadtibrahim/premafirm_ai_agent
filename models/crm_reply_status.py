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
        mail.mail, bulk, follow-up, AI service)."""
        res = super()._message_post_after_hook(message, msg_values)
        author = message.author_id
        now = fields.Datetime.now()
        if (message.message_type == 'email'
                and author and not author.partner_share):
            # An internal user's partner authored the email → outbound.
            # No parent/references ⇒ we initiated a new thread ⇒ outreach.
            vals = {'last_outbound_at': now}
            if not (message.parent_id or msg_values.get('references')
                    or msg_values.get('in_reply_to')):
                vals['last_outreach_at'] = now
            self.write(vals)
        elif (message.message_type == 'comment'
                and author and author.partner_share):
            # Customer-authored chatter (portal comment) = engagement.
            self.write({
                'last_inbound_at': now,
                'last_meaningful_reply_at': now,
                'last_inbound_classification': 'normal_reply',
            })
        return res
