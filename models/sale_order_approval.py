import hashlib
import base64
import logging

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.exceptions import InvalidSignature
from markupsafe import Markup

from odoo import models, fields, api, exceptions

_logger = logging.getLogger(__name__)


class ResCompanyKeys(models.Model):
    _inherit = 'res.company'

    x_rsa_private_key = fields.Text(string='RSA Private Key (PEM)', groups='base.group_system', copy=False)
    x_rsa_public_key = fields.Text(string='RSA Public Key (PEM)', copy=False)

    def _get_or_create_rsa_keys(self):
        self.ensure_one()
        if not self.x_rsa_private_key:
            self._generate_rsa_keys()
        return self.x_rsa_private_key, self.x_rsa_public_key

    def _generate_rsa_keys(self):
        self.ensure_one()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        self.sudo().write({
            'x_rsa_private_key': private_pem,
            'x_rsa_public_key': public_pem,
        })
        _logger.info('RSA-2048 key pair generated for company %s', self.name)


class SaleOrderApproval(models.Model):
    _name = 'sale.order.approval'
    _description = 'Customer Digital Approval Signature'
    _order = 'approved_at desc'

    sale_order_id = fields.Many2one('sale.order', required=True, ondelete='cascade', index=True)
    customer_name = fields.Char(required=True, readonly=True)
    approved_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    ip_address = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)
    approval_method = fields.Selection([
        ('portal', 'Customer Portal'),
        ('whatsapp', 'WhatsApp Button'),
    ], required=True, readonly=True)

    # Cryptographic fields
    signed_payload = fields.Char(readonly=True, help='Canonical string that was signed')
    document_hash = fields.Char(readonly=True, help='SHA-256 of the signed payload')
    signature_b64 = fields.Char(readonly=True, help='RSA signature (base64)')
    public_key_pem = fields.Text(readonly=True, help='Company public key at signing time')

    is_verified = fields.Boolean(compute='_compute_is_verified', store=False)

    # ── Signature generation ──────────────────────────────────────────────────

    def _build_payload(self, order, customer_name, approved_at, ip_address):
        ts = approved_at.strftime('%Y-%m-%dT%H:%M:%SZ') if approved_at else ''
        return '|'.join([
            order.name,
            str(round(order.amount_total, 2)),
            order.currency_id.name,
            order.partner_id.name or '',
            customer_name,
            ts,
            ip_address or '',
        ])

    @api.model
    def create_for_order(self, order, customer_name, ip_address, user_agent, method):
        company = order.company_id
        priv_pem, pub_pem = company._get_or_create_rsa_keys()

        approved_at = fields.Datetime.now()
        payload = self._build_payload(order, customer_name, approved_at, ip_address)
        doc_hash = hashlib.sha256(payload.encode()).hexdigest()

        private_key = serialization.load_pem_private_key(priv_pem.encode(), password=None)
        raw_sig = private_key.sign(
            payload.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(raw_sig).decode()

        rec = self.create({
            'sale_order_id': order.id,
            'customer_name': customer_name,
            'approved_at': approved_at,
            'ip_address': ip_address,
            'user_agent': (user_agent or '')[:255],
            'approval_method': method,
            'signed_payload': payload,
            'document_hash': doc_hash,
            'signature_b64': sig_b64,
            'public_key_pem': pub_pem,
        })
        _logger.info('Approval signature created for %s by %s (method=%s, ip=%s)',
                     order.name, customer_name, method, ip_address)
        return rec

    # ── Verification ─────────────────────────────────────────────────────────

    @api.depends('signature_b64', 'public_key_pem', 'signed_payload')
    def _compute_is_verified(self):
        for rec in self:
            rec.is_verified = rec._verify()

    def _verify(self):
        self.ensure_one()
        if not (self.signature_b64 and self.public_key_pem and self.signed_payload):
            return False
        try:
            pub_key = serialization.load_pem_public_key(self.public_key_pem.encode())
            pub_key.verify(
                base64.b64decode(self.signature_b64),
                self.signed_payload.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, Exception):
            return False

    def action_verify(self):
        self.ensure_one()
        if self._verify():
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Signature Valid',
                    'message': f'RSA-2048 signature verified. Signed by {self.customer_name} on '
                               f'{self.approved_at} from {self.ip_address}.',
                    'type': 'success',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Signature Invalid',
                'message': 'The signature could not be verified. The document may have been tampered with.',
                'type': 'danger',
                'sticky': True,
            },
        }


class SaleOrderApprovalMixin(models.Model):
    _inherit = 'sale.order'

    approval_signature_ids = fields.One2many(
        'sale.order.approval', 'sale_order_id', string='Approval Signatures')
    approval_signature_count = fields.Integer(compute='_compute_sig_count')
    x_customer_approved_at = fields.Datetime(
        string='Customer Approved At', readonly=True, copy=False)
    x_customer_approved_by = fields.Char(
        string='Approved By', readonly=True, copy=False)
    x_customer_approval_method = fields.Char(
        string='Approval Method', readonly=True, copy=False)
    x_customer_approval_ip = fields.Char(
        string='Approval IP', readonly=True, copy=False)

    @api.depends('approval_signature_ids')
    def _compute_sig_count(self):
        for rec in self:
            rec.approval_signature_count = len(rec.approval_signature_ids)

    def _register_customer_approval(self, customer_name, ip, user_agent, method):
        sig = self.env['sale.order.approval'].create_for_order(
            self, customer_name, ip, user_agent, method)
        self.write({
            'x_customer_approved_at': sig.approved_at,
            'x_customer_approved_by': customer_name,
            'x_customer_approval_method': dict(
                self.env['sale.order.approval']._fields['approval_method'].selection
            ).get(method, method),
            'x_customer_approval_ip': ip,
        })
        return sig

    def action_send_wa_for_approval(self):
        """Send the customer a WhatsApp message with the portal approval link.
        Sends directly via the Cloud API — no template wizard shown to staff.
        """
        self.ensure_one()

        if not self.access_token:
            self._portal_ensure_token()   # Odoo 18: underscore prefix

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        portal_link = f'{base_url}/my/quotes/{self.id}/sign/{self.access_token}'
        wa_text = '\n'.join(self._build_wa_approval_message(portal_link))

        # ── Route 1: via linked WA Negotiation channel (existing conversation) ──
        neg = self.env['premafirm.wa.negotiation'].sudo().search(
            [('sale_order_id', '=', self.id)], limit=1)
        if neg and neg.channel_id:
            neg._wa_send_text(wa_text)
            neg._log_entry('approval_link_sent', text='Portal approval link sent by staff')
            self._post_wa_chatter_note(portal_link)
            return self._wa_sent_notification()

        # ── Route 2: direct Cloud API send (no existing channel needed) ──
        partner = self.partner_id
        phone = (partner.mobile or partner.phone or '').strip().replace(' ', '').replace('-', '')
        if not phone:
            raise exceptions.UserError(
                'No mobile or phone number on the customer record. '
                'Please add one before sending via WhatsApp.')

        # Normalise to E.164 (strip leading 0, add country code if missing)
        if not phone.startswith('+'):
            phone = '+1' + phone.lstrip('0')  # default CA/US

        wa_account = self.env['whatsapp.account'].sudo().search(
            [('active', '=', True)], limit=1)
        if not wa_account:
            raise exceptions.UserError('No active WhatsApp account configured in settings.')

        sent = self._send_wa_direct(wa_account, phone, wa_text)
        self._post_wa_chatter_note(portal_link, sent=sent)

        return self._wa_sent_notification(sent=sent, phone=phone)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _send_wa_direct(self, wa_account, phone, text):
        """Send a free-form text message via the WhatsApp Cloud API."""
        import requests
        try:
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': phone,
                'type': 'text',
                'text': {'body': text, 'preview_url': True},
            }
            url = f'https://graph.facebook.com/v17.0/{wa_account.phone_uid}/messages'
            resp = requests.post(
                url,
                headers={
                    'Authorization': f'Bearer {wa_account.token}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=30,
            )
            if resp.ok:
                _logger.info('WA approval link sent to %s for order %s', phone, self.name)
                return True
            err = (resp.json().get('error') or {})
            _logger.warning('WA direct send failed for %s: %s',
                            self.name, err.get('message', resp.text[:200]))
            return False
        except Exception as e:
            _logger.warning('WA direct send error for %s: %s', self.name, e)
            return False

    def _post_wa_chatter_note(self, portal_link, sent=True):
        status = 'sent to customer via WhatsApp' if sent else 'could not be sent automatically'
        self.sudo().message_post(
            body=Markup(
                '<b>WhatsApp Approval Link {status}.</b><br/>'
                'Portal link: <a href="{link}">{link}</a><br/>'
                '<small>Customer can Approve &amp; Sign, Request Changes, or Cancel.</small>'
            ).format(status=status, link=portal_link),
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

    def _wa_sent_notification(self, sent=True, phone=''):
        if sent:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'WhatsApp Sent ✓',
                    'message': f'Approval link sent to customer'
                               + (f' ({phone})' if phone else '') + '.',
                    'type': 'success',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'WhatsApp Not Sent',
                'message': 'Could not send via WhatsApp — the portal link has been '
                           'posted in the chatter. You can copy and send it manually.',
                'type': 'warning',
                'sticky': True,
            },
        }

    def _build_wa_approval_message(self, portal_link):
        order = self
        lines = [
            f'Hello {order.partner_id.name},',
            '',
            f'Your quotation *{order.name}* from *PremaFirm Inc.* is ready for your review.',
            '',
        ]
        if order.order_line:
            first_line = order.order_line[0]
            lines.append(f'Service: {first_line.name[:80]}')
        lines += [
            f'Total: *{order.currency_id.symbol}{order.amount_total:,.2f}*',
            '',
            'Please tap the link below to review, approve, request changes, or cancel:',
            '',
            portal_link,
            '',
            'Your approval will confirm the booking. Thank you!',
        ]
        return lines
