"""PHASE 21 — email provider webhook endpoint (Resend / Amazon SES).

POST /premafirm/mail/webhook/<provider>  (JSON, public route, CSRF off —
external providers call it; the shared secret gates it)

Payload shapes supported (defensively — the ledger normalizes them):

* Resend:  {"type": "email.bounced", "data": {"id": <event id>,
            "email_id": <message id>, "email": {...}, "bounce": {...}}}
* SES:     {"eventType": "Bounce", "mail": {"messageId": <id>,
            "headers": [{"name": "Message-ID", "value": ...}]}}

The route is a thin shell: dedupe, correlation and application live in
``premafirm.mail.provider.event.process_provider_event`` so the battery
can exercise the whole pipeline without HTTP.

Secret: ir.config_parameter ``premafirm.mail.webhook_secret``. When set,
the ``X-Webhook-Secret`` header must match (constant-time compare).
Unset → the endpoint accepts (documented: PRODUCTION MUST SET IT).
"""
import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Resend event type → ledger event_type
_RESEND_TYPES = {
    'email.delivered': 'delivered',
    'email.bounced': 'bounced',
    'email.complained': 'complained',
    'email.opened': 'opened',
    'email.clicked': 'clicked',
    'email.failed': 'failed',
}
# SES eventType → ledger event_type
_SES_TYPES = {
    'Delivery': 'delivered',
    'Bounce': 'bounced',
    'Complaint': 'complained',
    'Open': 'opened',
    'Click': 'clicked',
    'Send': 'other',
    'Reject': 'failed',
}


class PremafirmMailWebhook(http.Controller):

    def _authorized(self, secret):
        """Constant-time secret check; no secret configured → open."""
        if not secret:
            return True
        header = request.httprequest.headers.get('X-Webhook-Secret', '')
        return hmac.compare_digest(header, secret)

    def _event_id(self, data):
        """A stable per-event id for dedupe: the provider's own event id
        when present, else message-id + type (SES has no event id)."""
        eid = (data.get('id') or data.get('event_id')
               or data.get('messageId'))
        if eid:
            return str(eid)
        return '%s-%s' % (data.get('created_at') or 'ts',
                          data.get('type') or data.get('eventType') or 'x')

    def _message_id(self, payload, data):
        """Best correlation key: an explicit Message-ID header, else the
        provider message id (SES mail.messageId / Resend email_id)."""
        mail = payload.get('mail') or {}
        for header in mail.get('headers') or []:
            if header.get('name', '').lower() == 'message-id':
                return header.get('value') or False
        return (data.get('email_id') or data.get('messageId')
                or mail.get('messageId') or False)

    def _parse_event(self, payload):
        """Normalize one payload dict → (event_id, event_type,
        provider_message_id) or None when unrecognized."""
        if payload.get('type'):
            event_type = _RESEND_TYPES.get(payload['type'])
            if event_type is None:
                event_type = 'other'
            data = payload.get('data') or {}
            return (self._event_id(data), event_type,
                    self._message_id(payload, data))
        if payload.get('eventType'):
            event_type = _SES_TYPES.get(payload['eventType'], 'other')
            data = payload.get('mail') or {}
            return (self._event_id(data), event_type,
                    self._message_id(payload, data))
        return None

    @http.route('/premafirm/mail/webhook/<string:provider>', type='json',
                auth='public', methods=['POST'], csrf=False)
    def mail_webhook(self, provider, **kwargs):
        secret = request.env['ir.config_parameter'].sudo().get_param(
            'premafirm.mail.webhook_secret', '')
        if not self._authorized(secret):
            _logger.warning('PHASE 21: webhook rejected (bad secret) for '
                            '%s', provider)
            return {'status': 'unauthorized'}
        # Odoo 18: request.get_json_data() is the jsonrequest replacement
        raw = request.get_json_data() if hasattr(request, 'get_json_data') \
            else getattr(request, 'jsonrequest', None)
        if not raw:
            return {'status': 'error', 'error': 'empty payload'}
        payloads = raw if isinstance(raw, list) else [raw]
        Event = request.env['premafirm.mail.provider.event'].sudo()
        processed = deduped = 0
        for payload in payloads:
            parsed = self._parse_event(payload)
            if not parsed:
                _logger.warning('PHASE 21: webhook payload not recognized '
                                'for %s: %s', provider, str(payload)[:200])
                continue
            event_id, event_type, message_id = parsed
            import json as _json
            _event, created = Event.process_provider_event(
                provider, event_id, event_type, message_id,
                payload=_json.dumps(payload)[:8000])
            processed += 1 if created else 0
            deduped += 0 if created else 1
        return {'status': 'ok', 'processed': processed,
                'deduped': deduped}
