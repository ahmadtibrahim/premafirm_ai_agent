import json
import logging

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class QuoteApprovalPortal(http.Controller):

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_order(self, order_id, access_token):
        order = request.env['sale.order'].sudo().browse(order_id)
        if not order.exists():
            return None
        try:
            order._portal_ensure_token()   # Odoo 18: underscore prefix
        except Exception:
            pass
        if order.access_token != access_token:
            return None
        return order

    def _client_ip(self):
        forwarded = request.httprequest.environ.get('HTTP_X_FORWARDED_FOR', '')
        return (forwarded.split(',')[0].strip()
                if forwarded else request.httprequest.remote_addr)

    # ── GET: show approval page ───────────────────────────────────────────────

    @http.route(
        '/my/quotes/<int:order_id>/sign/<string:access_token>',
        auth='public', website=True, methods=['GET'],
    )
    def portal_quote_sign(self, order_id, access_token, **kwargs):
        order = self._get_order(order_id, access_token)
        if not order:
            return request.not_found()
        if order.x_customer_approved_at:
            return request.render('premafirm_ai_engine.portal_quote_already_processed', {
                'order': order,
            })
        if order.state not in ('draft', 'sent'):
            return request.render('premafirm_ai_engine.portal_quote_already_processed', {
                'order': order,
            })
        return request.render('premafirm_ai_engine.portal_quote_sign', {
            'order': order,
            'access_token': access_token,
            'partner_name': order.partner_id.name or '',
        })

    # ── POST: process action ─────────────────────────────────────────────────

    @http.route(
        '/my/quotes/<int:order_id>/sign/submit',
        auth='public', website=True, methods=['POST'], csrf=True,
    )
    def portal_quote_sign_submit(self, order_id, **kwargs):
        order = self._get_order(order_id, kwargs.get('access_token', ''))
        if not order:
            return request.make_json_response({'error': 'Invalid or expired link.'}, status=403)

        action = kwargs.get('action')
        customer_name = (kwargs.get('customer_name') or order.partner_id.name or '').strip()
        ip = self._client_ip()
        ua = request.httprequest.user_agent.string[:255]

        if action == 'approve':
            if order.state not in ('draft', 'sent'):
                return request.make_json_response(
                    {'error': 'Quotation has already been processed.'}, status=409)

            # Portal approval records acceptance only. Converting the quote to
            # a Sales Order, booking it, and contacting the customer remain
            # explicit internal actions.
            sig, approval_created = order._register_customer_approval(
                customer_name, ip, ua, 'portal'
            )

            if not approval_created:
                return request.make_json_response({
                    'success': True,
                    'order_name': order.name,
                    'approved_at': fields.Datetime.to_string(sig.approved_at),
                    'signature_hash': sig.document_hash[:16] + '…',
                    'customer_name': sig.customer_name,
                    'status': 'pending_internal_confirmation',
                    'already_approved': True,
                })

            # Update linked WA negotiation status → approved
            neg = request.env['premafirm.wa.negotiation'].sudo().search(
                [('sale_order_id', '=', order.id)], limit=1)
            if neg:
                neg.write({'status': 'approved'})
                neg._log_entry('approved_portal',
                               text=(f'Digitally signed by {customer_name} via portal '
                                     f'(IP: {ip}); pending internal confirmation'))

            # Chatter note
            order.sudo().message_post(
                body=f'<b>Customer Approval Received</b><br/>'
                     f'Approved by: <b>{customer_name}</b><br/>'
                     f'Method: Portal (digital signature)<br/>'
                     f'IP: {ip}<br/>'
                     f'Signature hash: <code>{sig.document_hash[:32]}…</code><br/>'
                     f'<b>Status: Pending internal review and confirmation.</b>',
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

            return request.make_json_response({
                'success': True,
                'order_name': order.name,
                'approved_at': fields.Datetime.to_string(sig.approved_at) + ' UTC',
                'signature_hash': sig.document_hash[:16] + '…',
                'customer_name': customer_name,
                'status': 'pending_internal_confirmation',
            })

        elif action == 'edit':
            edit_note = (kwargs.get('edit_note') or '').strip()
            if not edit_note:
                return request.make_json_response(
                    {'error': 'Please describe what you would like to change.'}, status=400)

            neg = request.env['premafirm.wa.negotiation'].sudo().search(
                [('sale_order_id', '=', order.id)], limit=1)
            if neg:
                neg._request_quote_edits(customer_text=edit_note)
                neg._handle_customer_edit_request(edit_note)
            else:
                order.sudo().message_post(
                    body=f'<b>Customer requested changes (portal):</b><br/>{edit_note}',
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
            return request.make_json_response({'success': True, 'action': 'edit'})

        elif action == 'cancel':
            cancel_reason = (kwargs.get('cancel_reason') or '').strip()
            neg = request.env['premafirm.wa.negotiation'].sudo().search(
                [('sale_order_id', '=', order.id)], limit=1)
            if neg:
                neg._cancel_linked_quotation(
                    reason=cancel_reason or 'Customer cancelled via portal')
            elif order.state in ('draft', 'sent'):
                order.sudo().action_cancel()
                order.sudo().message_post(
                    body=f'<b>Customer cancelled quotation via portal.</b>'
                         + (f'<br/>Reason: {cancel_reason}' if cancel_reason else ''),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
            return request.make_json_response({'success': True, 'action': 'cancel'})

        return request.make_json_response({'error': 'Unknown action.'}, status=400)
