"""PHASE 21 — email provider event ledger (Resend/SES webhooks).

Every webhook event is recorded once, deduplicated at the DATABASE level
(provider + event id + type), correlated to the Odoo records via the RFC
Message-ID that survives the provider rewrite, and applied idempotently:

* ``bounced``      — bulk queue item → bounced; recipient blacklisted;
                     the lead gets the bounce classification (NEVER an
                     engagement/reply stamp),
* ``complained``   — bulk queue item → complaint_received (the PHASE 10
                     placeholder); recipient blacklisted,
* ``delivered`` / ``opened`` / ``clicked`` / ``failed`` — ledger-only
                     (informational, no state changes).

Events that cannot be correlated are kept (state ``unresolved``) with
their raw payload for human review — nothing is ever dropped silently.

The webhook endpoint is controllers/provider_webhook.py; this model is
the single place dedupe + correlation + application live, so the battery
can exercise the whole pipeline without HTTP.
"""
import logging

from odoo import api, fields, models
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)


class PremafirmMailProviderEvent(models.Model):
    _name = 'premafirm.mail.provider.event'
    _description = 'Email Provider Event (webhook ledger)'
    _order = 'received_at desc, id desc'

    provider = fields.Selection(
        [('resend', 'Resend'), ('amazon_ses', 'Amazon SES')],
        default='resend', required=True)
    provider_event_id = fields.Char('Provider Event ID', required=True,
                                    index=True)
    event_type = fields.Selection([
        ('delivered', 'Delivered'),
        ('bounced', 'Bounced'),
        ('complained', 'Complained'),
        ('opened', 'Opened'),
        ('clicked', 'Clicked'),
        ('failed', 'Failed'),
        ('other', 'Other'),
    ], default='other', required=True, index=True)
    provider_message_id = fields.Char('Provider Message-ID', index=True)
    message_record_id = fields.Many2one(
        'mail.message', 'Thread Message', ondelete='set null', index=True)
    mail_id = fields.Many2one(
        'mail.mail', 'Mail Record', ondelete='set null', index=True)
    lead_id = fields.Many2one(
        'crm.lead', 'Lead', ondelete='set null', index=True)
    queue_id = fields.Many2one(
        'premafirm.crm.bulk.email.queue', 'Bulk Queue Item',
        ondelete='set null', index=True)
    received_at = fields.Datetime(
        'Received At', required=True, index=True,
        default=lambda self: fields.Datetime.now())
    payload = fields.Text('Raw Payload')
    state = fields.Selection([
        ('recorded', 'Recorded'),
        ('applied', 'Applied'),
        ('unresolved', 'Unresolved'),
    ], default='recorded', required=True, index=True)

    _sql_constraints = [
        ('provider_event_unique',
         'UNIQUE(provider, provider_event_id, event_type)',
         'This provider event was already recorded — webhook replay is '
         'deduplicated at the database level.'),
    ]

    # ── entry point ─────────────────────────────────────────────────

    @api.model
    def process_provider_event(self, provider, event_id, event_type,
                               provider_message_id=False, payload=False):
        """Record, correlate, apply — idempotent for replay.

        Returns (event, created): a redelivered event returns the ORIGINAL
        record with created=False, without re-applying (the unique
        constraint guards the search-then-create race, catching
        IntegrityError)."""
        provider = (provider or 'resend').strip().lower()
        if provider == 'ses':
            provider = 'amazon_ses'
        norm = self.search([
            ('provider', '=', provider),
            ('provider_event_id', '=', event_id),
            ('event_type', '=', event_type),
        ], limit=1)
        if norm:
            return norm, False
        vals = {
            'provider': provider,
            'provider_event_id': event_id,
            'event_type': event_type,
            'provider_message_id': (provider_message_id or '').strip()[:512],
            'payload': payload or False,
        }
        try:
            event = self.sudo().create(vals)
        except Exception:
            # unique-constraint race: a concurrent delivery of the same
            # event already won — return the winner, apply nothing.
            self.env.cr.rollback()
            _logger.info('PHASE 21: duplicate event %s/%s/%s replayed',
                         provider, event_id, event_type)
            return self.search([
                ('provider', '=', provider),
                ('provider_event_id', '=', event_id),
                ('event_type', '=', event_type),
            ], limit=1), False
        event._correlate()
        event._apply()
        return event, True

    # ── correlation ─────────────────────────────────────────────────

    def _correlate(self):
        """Resolve the provider message-id to the Odoo records.

        The correlation key is the RFC 5322 Message-ID — the identifier
        that survives the Resend/SES rewrite (PHASE 1 findings) and the
        one stored on mail.message.message_id."""
        self.ensure_one()
        # mail.message.message_id stores the RFC header verbatim — WITH
        # angle brackets — while providers may deliver it bare (SES
        # messageId) or bracketed (a Message-ID header). Normalize to the
        # stored form before matching.
        mid = (self.provider_message_id or '').strip()
        if mid and not mid.startswith('<'):
            mid = '<%s>' % mid
        if not mid:
            self.state = 'unresolved'
            return
        msg = self.env['mail.message'].sudo().search(
            [('message_id', '=', mid)], limit=1)
        if msg:
            self.message_record_id = msg.id
            if msg.model == 'crm.lead':
                self.lead_id = msg.res_id
            mail = self.env['mail.mail'].sudo().search(
                [('mail_message_id', '=', msg.id)], limit=1)
            if mail:
                self.mail_id = mail.id
                queue = self.env['premafirm.crm.bulk.email.queue'].sudo() \
                    .search([('mail_id', '=', mail.id)], limit=1)
                if queue:
                    self.queue_id = queue.id
        if not self.message_record_id:
            self.state = 'unresolved'
            return

    # ── application (idempotent: runs exactly once per event) ───────

    def _apply(self):
        """Apply the event's side effects. Only runs at creation; replay
        is deduplicated before this point, so no state guard is needed."""
        self.ensure_one()
        if self.state == 'unresolved':
            return
        if self.event_type in ('delivered', 'opened', 'clicked', 'failed',
                               'other'):
            self.state = 'applied'
            return
        # the recipient to suppress for bounce/complaint
        recipient = False
        if self.queue_id:
            recipient = self.queue_id.email_to or False
        elif self.lead_id:
            recipient = self.lead_id.email_from or False
        norm = email_normalize(recipient) if recipient else False
        if self.event_type in ('bounced', 'complained'):
            # never email this address again — a provider bounce/complaint
            # is the strongest deliverability signal we get
            if norm:
                blacklist = self.env['mail.blacklist'].sudo()
                if not blacklist.search([('email', '=', norm)], limit=1):
                    blacklist.create({'email': norm})
                partner = self.env['res.partner'].sudo().search(
                    [('email_normalized', '=', norm)], limit=1)
                if partner and not partner.is_blacklisted:
                    partner.write({'is_blacklisted': True})
        if self.event_type == 'bounced':
            if self.queue_id and self.queue_id.state in (
                    'queued', 'sending', 'sent'):
                # a replied item stays replied — the reply happened before
                # the provider bounce and is real engagement
                self.queue_id.write({
                    'state': 'bounced',
                    'error_msg': 'Provider bounce (webhook %s)'
                                 % self.provider_event_id,
                })
            # bounce classification on the lead — NEVER an engagement or
            # reply stamp (bounces never count, PHASE 4-5 rule)
            if self.lead_id and not self.lead_id.last_meaningful_reply_at:
                self.lead_id.write(
                    {'last_inbound_classification': 'bounce'})
        elif self.event_type == 'complained':
            if self.queue_id and not self.queue_id.complaint_received:
                self.queue_id.write({'complaint_received': True})
        self.state = 'applied'
        _logger.info('PHASE 21: %s event %s applied (mail %s, lead %s)',
                     self.event_type, self.provider_event_id,
                     self.mail_id.id or '-', self.lead_id.id or '-')
