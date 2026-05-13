"""
Extend whatsapp.account._process_messages to handle 'interactive' message type.

When a customer clicks an Approve ✅ / Cancel ❌ button on a quotation message,
WhatsApp sends a webhook with type='interactive' and interactive.type='button_reply'.
The enterprise module ignores this type, so we pre-process it to 'text' before
calling super, which lets it flow through to our discuss_channel_ml.py hook.
"""
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class WhatsAppAccountML(models.Model):
    _inherit = 'whatsapp.account'

    def _process_messages(self, value):
        # Pre-process interactive button_reply / list_reply → convert to text
        # so the base _process_messages can route it to the channel as a normal message.
        for msg in value.get('messages', []):
            if msg.get('type') != 'interactive':
                continue
            interactive = msg.get('interactive', {})
            itype = interactive.get('type', '')
            if itype == 'button_reply':
                title = interactive['button_reply'].get('title', '')
                msg['type'] = 'text'
                msg['text'] = {'body': title}
                _logger.info('WA interactive button_reply converted to text: %r', title)
            elif itype == 'list_reply':
                title = interactive['list_reply'].get('title', '')
                msg['type'] = 'text'
                msg['text'] = {'body': title}
            else:
                _logger.debug('WA interactive type %r not handled, skipping', itype)

        super()._process_messages(value)
