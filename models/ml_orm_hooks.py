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
    x_meaningful_activity_at = fields.Datetime(
        string='Last Meaningful CRM Activity',
        compute='_compute_meaningful_activity_at',
        compute_sudo=True,
        store=True,
        index=True,
        help='PHASE 41: last meaningful CRM interaction for the wait-queue '
             'sort — customer email, sales outbound, human note, or call. '
             'Untouched leads (no meaningful activity) fall back to '
             'create_date so they rise to the TOP of their stage. Excludes '
             'OdooBot/system chatter, mt_note tracking noise, and '
             'technical notifications.',
    )

    @api.depends('x_last_outreach_at', 'x_reply_received_at', 'create_date')
    def _compute_meaningful_activity_at(self):
        """Oldest-waiting-first sort timestamp.

        Queue semantics: HOW LONG HAS THIS PROSPECT BEEN WAITING FOR HUMAN
        SALES ATTENTION?  = max(last meaningful thread message date,
        x_reply_received_at, x_last_outreach_at); untouched leads use their
        create_date (born waiting → top of the stage).  System noise is
        excluded at the message level: mt_note tracking chatter, OdooBot
        (partner root) messages, notifications.  Deterministic: the stored
        value is never NULL, so SQL NULL ordering is never in play.
        """
        if not self:
            return
        now = _dt.now()
        Note = self.env.ref('mail.mt_note', raise_if_not_found=False)
        bot_partner_id = self.env.ref('base.partner_root',
                                      raise_if_not_found=False).id if (
            self.env.ref('base.partner_root', raise_if_not_found=False)
        ) else False
        MSG = self.env['mail.message'].sudo()
        msg_max = {}
        try:
            # One grouped query for the whole batch; message_type email
            # (inbound), email_outgoing (sales outbound) and comment
            # (human chatter) are meaningful; mt_note + OdooBot are not.
            domain = [('model', '=', 'crm.lead'),
                      ('res_id', 'in', self.ids),
                      ('message_type', 'in',
                       ('email', 'email_outgoing', 'comment'))]
            if Note:
                domain.append(('subtype_id', '!=', Note.id))
            if bot_partner_id:
                domain.append(('author_id', '!=', bot_partner_id))
            for d in MSG.search_read(domain, ['res_id', 'date'],
                                     order='date desc'):
                rid = d['res_id']
                if rid not in msg_max or (d['date'] or now) > msg_max[rid]:
                    msg_max[rid] = d['date'] or now
        except Exception as exc:
            # Sorting must never break a lead save; degrade to fields only.
            _logger.warning('meaningful-activity scan failed: %s', exc)
        for lead in self:
            candidates = [msg_max.get(lead.id),
                          lead.x_reply_received_at,
                          lead.x_last_outreach_at]
            good = [c for c in candidates if c]
            lead.x_meaningful_activity_at = max(good) if good else (
                lead.create_date or now)

    @api.depends('x_last_outreach_at', 'create_date')
    def _compute_kanban_sort_date(self):
        for lead in self:
            lead.x_kanban_sort_date = lead.x_last_outreach_at or lead.create_date

    @api.depends('x_needs_attention', 'x_attention_at')
    def _compute_attention_reply_sort_at(self):
        for lead in self:
            lead.x_attention_reply_sort_at = lead.x_attention_at if lead.x_needs_attention else False

    # PHASE 41: the pipeline is a WAIT QUEUE — oldest meaningful activity
    # first, newest last. Needs Attention is a VISUAL badge only (rendered
    # on the card), it no longer controls ordering. Untouched leads carry
    # create_date in x_meaningful_activity_at and rise to the top.
    _order = 'x_meaningful_activity_at asc, create_date asc, id asc'

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
