"""
Phase 2 — ORM Hooks
Extend core models to queue ML ingestion on relevant write/create/post events.
IMPORTANT: all hooks are wrapped in try/except — they must NEVER break the save operation.
No GPT calls here — only queue raw structured data.
"""
import json
import logging
from datetime import datetime as _dt

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CrmLeadMLHooks(models.Model):
    _inherit = 'crm.lead'

    # ── Extra fields for contact rotation / outreach tracking ────
    x_last_outreach_at = fields.Datetime(string='Last Outreach At')
    x_response_status = fields.Selection(
        [
            ('none', 'None'),
            ('replied', 'Replied'),
            ('bounced', 'Bounced'),
            ('unsubscribed', 'Unsubscribed'),
        ],
        default='none',
        string='Response Status',
    )
    x_rotation_count = fields.Integer(default=0, string='Rotation Count')
    x_snov_enrichment_requested = fields.Boolean(
        string='Snov.io Searched',
        default=False,
        help='Set to True after Snov.io has been called for this lead to prevent duplicate API calls.',
    )
    x_contact_rotation_ids = fields.One2many(
        'premafirm.crm.contact.rotation',
        'lead_id',
        string='Contact Rotations',
    )
    x_needs_attention = fields.Boolean(
        string='Needs Attention',
        default=False,
        index=True,
        help='Set when customer replies; cleared when salesperson sends next email.',
    )
    x_referred_by_partner_id = fields.Many2one(
        'res.partner',
        string='Referred By',
        help='Original contact who referred this outreach to another contact.',
    )
    x_reply_received_at = fields.Datetime(
        string='Reply Received At',
        index=True,
        help='Timestamp of the last inbound customer email. Used for 3-day/6-day follow-up timers.',
    )
    x_attention_at = fields.Datetime(
        string='Attention At',
        index=True,
        help='Timestamp of the latest event that should push the lead to the top: customer reply or new assignment.',
    )
    x_attention_reason = fields.Selection(
        [
            ('reply', 'Reply'),
            ('assignment', 'Assignment'),
        ],
        string='Attention Reason',
        help='Why this lead is currently marked as needing attention.',
    )
    x_attention_reply_sort_at = fields.Datetime(
        string='Attention Sort At',
        compute='_compute_attention_reply_sort_at',
        store=True,
        index=True,
        help='Used only for active attention sorting. Cleared as soon as the lead no longer needs attention.',
    )
    x_kanban_sort_date = fields.Datetime(
        string='Kanban Sort Date',
        compute='_compute_kanban_sort_date',
        store=True,
        index=True,
        help='Oldest leads at top: x_last_outreach_at when set, create_date as fallback. Never uses epoch so old never-contacted leads sort ahead of new ones.',
    )

    @api.depends('x_last_outreach_at', 'create_date')
    def _compute_kanban_sort_date(self):
        for lead in self:
            lead.x_kanban_sort_date = lead.x_last_outreach_at or lead.create_date

    @api.depends('x_needs_attention', 'x_attention_at')
    def _compute_attention_reply_sort_at(self):
        for lead in self:
            lead.x_attention_reply_sort_at = lead.x_attention_at if lead.x_needs_attention else False

    # Needs-attention leads (customer replied) always bubble to the top, most
    # recent reply first; everything else falls back to oldest-outreach-first.
    _order = 'x_needs_attention desc, x_attention_reply_sort_at desc, x_kanban_sort_date asc, id asc'

    def read(self, fields=None, load='_classic_read'):
        # Clear the flashing "needs attention" flag when the assigned
        # salesperson actually opens this specific lead. Guarded tightly:
        # - len(self) == 1: kanban/list always read a batch of ids, only a
        #   form view reads exactly one, so this is never true for a list/kanban render.
        # - not self.env.su: excludes cron jobs / any .sudo() internal call.
        # - self.env.uid == self.user_id.id: only the record's own assigned
        #   salesperson clears it by viewing it -- not admin, not a colleague
        #   browsing, not an unrelated background process touching the record.
        # Without these guards this fired on ANY internal single-record read
        # (crons, compute methods, etc.) and silently cleared the flag with
        # nobody ever having looked at the lead -- caught in testing 2026-07-07.
        #
        # Extended 2026-07-15: team-queue leads (e.g. website "Call Back" requests)
        # are created unassigned (user_id is False) so any team member can pick one
        # up -- the strict self.user_id.id check above never matches for those, so
        # opening one never cleared the flag. For an unassigned lead, also allow any
        # member of the lead's own sales team to clear it by opening it (still
        # excludes unrelated colleagues outside that team).
        result = super().read(fields=fields, load=load)
        if len(self) == 1 and self.x_needs_attention and not self.env.su:
            is_assigned_owner = self.env.uid == self.user_id.id
            is_team_member_on_unassigned = (
                not self.user_id
                and self.team_id
                and self.env.uid in self.team_id.crm_team_member_ids.user_id.ids
            )
            if is_assigned_owner or is_team_member_on_unassigned:
                try:
                    self.sudo().write({'x_needs_attention': False})
                except Exception as exc:
                    _logger.debug('Failed to clear x_needs_attention on read for lead %s: %s', self.id, exc)
        return result

    def action_snov_escalate_now(self):
        """Manual trigger: search Snov.io for a new contact and create a Suggestion lead.
        Can be called directly from the lead form — bypasses the cron timing check.
        """
        self.ensure_one()
        # Reset the guard so a manual re-run is always allowed
        self.sudo().write({'x_snov_enrichment_requested': False})

        primary_partner = self.partner_id
        if not primary_partner:
            return self._snov_warn('No contact linked to this lead.')

        company_partner = (
            primary_partner.parent_id if primary_partner.parent_id
            else (primary_partner if primary_partner.is_company else None)
        )
        if not company_partner:
            return self._snov_warn('No company found for this lead.')

        rotation_model = self.env['premafirm.crm.contact.rotation']
        rotation_model._escalate_to_snov(self, primary_partner, company_partner)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _snov_warn(self, msg):
        self.message_post(body=f'<b>Snov.io:</b> {msg}', subtype_xmlid='mail.mt_note')
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def write(self, vals):
        previous_user_ids = {}
        if 'user_id' in vals and not self.env.context.get('skip_assignment_attention'):
            previous_user_ids = {lead.id: lead.user_id.id for lead in self}

        # Skip queuing when only updating outreach timestamp
        if list(vals.keys()) == ['x_last_outreach_at']:
            return super().write(vals)

        result = super().write(vals)

        if previous_user_ids:
            attention_at = fields.Datetime.now()
            for lead in self:
                previous_user_id = previous_user_ids.get(lead.id)
                current_user_id = lead.user_id.id
                if not current_user_id or current_user_id == previous_user_id:
                    continue
                try:
                    lead.with_context(skip_assignment_attention=True).sudo().write({
                        'x_needs_attention': True,
                        'x_attention_at': attention_at,
                        'x_attention_reason': 'assignment',
                    })
                except Exception as exc:
                    _logger.debug('CRM lead assignment attention hook error on lead %s: %s', lead.id, exc)

        # Queue on stage change or probability change
        if 'stage_id' in vals or 'probability' in vals:
            for lead in self:
                try:
                    stage_name = lead.stage_id.name if lead.stage_id else ''
                    contact_name = lead.partner_id.name if lead.partner_id else lead.partner_name or ''
                    input_ctx = (
                        f'Lead: {lead.name}\n'
                        f'Stage: {stage_name}\n'
                        f'Contact: {contact_name}\n'
                        f'Probability: {lead.probability}\n'
                        f'Company: {lead.partner_id.parent_id.name if lead.partner_id and lead.partner_id.parent_id else ""}'
                    )
                    good_out = lead.description or lead.name or ''
                    lead._queue_ingest(
                        operation='stage_change',
                        knowledge_type='crm_reply',
                        input_context=input_ctx,
                        good_output=good_out,
                        source_ref=f'crm.lead:{lead.id}',
                    )
                except Exception as exc:
                    _logger.debug('CRM lead write hook error: %s', exc)

        return result

    def _queue_ingest(self, operation, knowledge_type, input_context, good_output, source_ref, weight=1.0):
        """Create a queue item for async ML ingestion. Never raises."""
        try:
            payload = json.dumps({
                'knowledge_type': knowledge_type,
                'input_context': input_context or '',
                'good_output': good_output or '',
                'source_ref': source_ref,
                'weight': weight,
            })
            self.env['premafirm.ml.ingest.queue'].sudo().create({
                'model_name': self._name,
                'record_id': self.id if len(self) == 1 else 0,
                'operation': operation,
                'payload': payload,
            })
        except Exception as exc:
            _logger.debug('_queue_ingest failed silently: %s', exc)


class AccountMoveMLHooks(models.Model):
    _inherit = 'account.move'

    def action_post(self):
        result = super().action_post()
        for move in self:
            try:
                if move.move_type not in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund'):
                    continue
                vendor_name = move.partner_id.name if move.partner_id else ''
                input_ctx = (
                    f'Invoice: {move.name}\n'
                    f'Vendor/Customer: {vendor_name}\n'
                    f'Amount: {move.amount_total} {move.currency_id.name}\n'
                    f'Date: {move.invoice_date}\n'
                    f'Type: {move.move_type}'
                )
                line_desc = '; '.join(
                    filter(None, [ln.name for ln in move.invoice_line_ids[:10]])
                )
                self._queue_ingest_move(
                    move_id=move.id,
                    operation='post',
                    knowledge_type='bill_import',
                    input_context=input_ctx,
                    good_output=line_desc or input_ctx,
                    source_ref=f'account.move:{move.id}',
                )
            except Exception as exc:
                _logger.debug('AccountMove post hook error: %s', exc)
        return result

    def _queue_ingest_move(self, move_id, operation, knowledge_type, input_context, good_output, source_ref, weight=1.0):
        """Queue a move for ML ingestion. Never raises."""
        try:
            payload = json.dumps({
                'knowledge_type': knowledge_type,
                'input_context': input_context or '',
                'good_output': good_output or '',
                'source_ref': source_ref,
                'weight': weight,
            })
            self.env['premafirm.ml.ingest.queue'].sudo().create({
                'model_name': 'account.move',
                'record_id': move_id,
                'operation': operation,
                'payload': payload,
            })
        except Exception as exc:
            _logger.debug('_queue_ingest_move failed silently: %s', exc)


class ResPartnerMLHooks(models.Model):
    _inherit = 'res.partner'

    def write(self, vals):
        result = super().write(vals)
        if any(f in vals for f in ('email', 'phone', 'function')):
            for partner in self:
                try:
                    input_ctx = (
                        f'Partner: {partner.name}\n'
                        f'Email: {partner.email or ""}\n'
                        f'Phone: {partner.phone or ""}\n'
                        f'Job Title: {partner.function or ""}\n'
                        f'Company: {partner.parent_id.name if partner.parent_id else ""}'
                    )
                    self._queue_ingest_partner(
                        partner_id=partner.id,
                        operation='write',
                        knowledge_type='customer_tag',
                        input_context=input_ctx,
                        good_output=partner.name,
                        source_ref=f'res.partner:{partner.id}',
                    )
                except Exception as exc:
                    _logger.debug('ResPartner write hook error: %s', exc)
        return result

    def _queue_ingest_partner(self, partner_id, operation, knowledge_type, input_context, good_output, source_ref, weight=1.0):
        """Queue a partner for ML ingestion. Never raises."""
        try:
            payload = json.dumps({
                'knowledge_type': knowledge_type,
                'input_context': input_context or '',
                'good_output': good_output or '',
                'source_ref': source_ref,
                'weight': weight,
            })
            self.env['premafirm.ml.ingest.queue'].sudo().create({
                'model_name': 'res.partner',
                'record_id': partner_id,
                'operation': operation,
                'payload': payload,
            })
        except Exception as exc:
            _logger.debug('_queue_ingest_partner failed silently: %s', exc)


class SaleOrderMLHooks(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        result = super().action_confirm()
        for order in self:
            try:
                input_ctx = (
                    f'Order: {order.name}\n'
                    f'Customer: {order.partner_id.name if order.partner_id else ""}\n'
                    f'Amount: {order.amount_total} {order.currency_id.name}\n'
                    f'Date: {order.date_order}'
                )
                line_info = '; '.join(
                    filter(None, [f'{ln.product_id.name} x{ln.product_uom_qty}' for ln in order.order_line[:10]])
                )
                self._queue_ingest_order(
                    order_id=order.id,
                    operation='post',
                    knowledge_type='rate_quote',
                    input_context=input_ctx,
                    good_output=line_info or input_ctx,
                    source_ref=f'sale.order:{order.id}',
                )
            except Exception as exc:
                _logger.debug('SaleOrder confirm hook error: %s', exc)
        return result

    def _queue_ingest_order(self, order_id, operation, knowledge_type, input_context, good_output, source_ref, weight=1.0):
        """Queue a sale order for ML ingestion. Never raises."""
        try:
            payload = json.dumps({
                'knowledge_type': knowledge_type,
                'input_context': input_context or '',
                'good_output': good_output or '',
                'source_ref': source_ref,
                'weight': weight,
            })
            self.env['premafirm.ml.ingest.queue'].sudo().create({
                'model_name': 'sale.order',
                'record_id': order_id,
                'operation': operation,
                'payload': payload,
            })
        except Exception as exc:
            _logger.debug('_queue_ingest_order failed silently: %s', exc)


class MailMessageMLHooks(models.Model):
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for msg in records:
            try:
                if msg.model not in ('crm.lead', 'res.partner'):
                    continue
                if msg.message_type != 'comment':
                    continue
                body_text = (msg.body or '').replace('<br>', '\n').replace('<br/>', '\n')
                # Strip HTML tags simply
                import re
                body_text = re.sub(r'<[^>]+>', '', body_text).strip()
                if not body_text or len(body_text) < 10:
                    continue

                body_limited = body_text[:500]
                input_ctx = (
                    f'Model: {msg.model} ID: {msg.res_id}\n'
                    f'Author: {msg.author_id.name if msg.author_id else ""}\n'
                    f'Message: {body_limited}'
                )
                payload = json.dumps({
                    'knowledge_type': 'crm_reply',
                    'input_context': input_ctx,
                    'good_output': body_limited,
                    'source_ref': f'mail.message:{msg.id}',
                    'weight': 0.8,
                })
                self.env['premafirm.ml.ingest.queue'].sudo().create({
                    'model_name': msg.model,
                    'record_id': msg.res_id or 0,
                    'operation': 'post',
                    'payload': payload,
                })
            except Exception as exc:
                _logger.debug('MailMessage create hook error: %s', exc)
        return records
