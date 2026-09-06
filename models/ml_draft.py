import json as _json
import logging
from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


DRAFT_TYPES = [
    ('rate_quote',    'Rate Quote'),
    ('wa_reply',      'WhatsApp Reply'),
    ('crm_reply',     'CRM Reply'),
    ('invoice_flag',  'Invoice Flag'),
    ('customer_tag',  'Customer Tag'),
    ('load_tender',   'Load Tender'),
    ('bill_autofill',      'Bill Auto-Fill'),
    ('rate_conf_autofill', 'Rate Conf Auto-Fill'),
]

STATUS = [
    ('pending',   'Pending Review'),
    ('approved',  'Approved'),
    ('edited',    'Approved with Edits'),
    ('rejected',  'Rejected'),
    ('sent',      'Sent'),
]


class PremafirmMLDraft(models.Model):
    """
    Every AI suggestion lands here first. A human reviews it:
      - Approve as-is → goes to knowledge base with weight 2.0
      - Edit + approve → human_final is used, feedback_note teaches the AI why
      - Reject → feedback_note is required, goes to knowledge base as a negative example
    """
    _name = 'premafirm.ml.draft'
    _description = 'ML Draft Suggestion'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(compute='_compute_name', store=True)
    draft_type = fields.Selection(DRAFT_TYPES, required=True, index=True)
    status = fields.Selection(STATUS, default='pending', index=True, tracking=True)

    # Source record
    source_model   = fields.Char(index=True)
    source_id      = fields.Integer(index=True)
    source_ref     = fields.Char(compute='_compute_source_ref', store=False)
    negotiation_id = fields.Many2one('premafirm.wa.negotiation', string='Negotiation',
                                     ondelete='set null')

    # AI output
    ai_suggestion = fields.Text('AI Suggestion', required=True)
    ai_suggestion_display = fields.Text(
        string="AI Suggestion (Readable)",
        compute="_compute_ai_suggestion_display",
    )
    ai_reasoning  = fields.Text('AI Reasoning',
        help='Why the AI produced this suggestion — which examples influenced it.')
    examples_used = fields.Integer('Examples Used', default=0,
        help='How many knowledge-base entries the AI used to produce this draft.')

    # Human review
    human_final  = fields.Text('Final Version',
        help='Fill this in if you want to change the suggestion before approving.')
    feedback_note = fields.Text('Feedback Note',
        help='WHY did you change or reject this? Even a short note helps the AI improve. '
             'e.g. "Rate is too low for refrigerated cargo" or "Tone too formal for this client".')
    reviewed_by  = fields.Many2one('res.users', readonly=True)
    reviewed_at  = fields.Datetime(readonly=True)

    # Context snapshot (JSON) — stored so we know what data was used
    context_snapshot = fields.Text('Context Snapshot')

    @api.depends('draft_type', 'source_model', 'source_id', 'create_date')
    def _compute_name(self):
        type_labels = dict(DRAFT_TYPES)
        for rec in self:
            label = type_labels.get(rec.draft_type, rec.draft_type or 'Draft')
            date  = rec.create_date.strftime('%b %d %Y') if rec.create_date else ''
            rec.name = f"{label} — {date}"

    def _compute_source_ref(self):
        for rec in self:
            if rec.source_model and rec.source_id:
                try:
                    target = self.env[rec.source_model].browse(rec.source_id)
                    rec.source_ref = target.display_name if target.exists() else f"{rec.source_model}/{rec.source_id}"
                except Exception:
                    rec.source_ref = f"{rec.source_model}/{rec.source_id}"
            else:
                rec.source_ref = ''

    # ── Actions ────────────────────────────────────────────────────

    def action_approve(self):
        """Approve the AI suggestion as-is — highest learning signal."""
        for rec in self:
            rec.write({
                'status': 'approved',
                'human_final': rec.ai_suggestion,
                'reviewed_by': self.env.uid,
                'reviewed_at': fields.Datetime.now(),
            })
            rec._learn(origin='approved', weight=2.0)
        return True

    def action_approve_edited(self):
        """Approve with the human-edited version in human_final."""
        for rec in self:
            if not rec.human_final:
                raise exceptions.UserError(
                    'Please fill in the "Final Version" field with your edited text before approving.')
            rec.write({
                'status': 'edited',
                'reviewed_by': self.env.uid,
                'reviewed_at': fields.Datetime.now(),
            })
            rec._learn(origin='edited', weight=1.5)
        return True

    def action_reject(self):
        for rec in self:
            if not rec.feedback_note:
                raise exceptions.UserError(
                    'Please leave a Feedback Note explaining why you rejected this. '
                    'It helps the AI learn and avoid repeating the mistake.')
            rec.write({
                'status': 'rejected',
                'reviewed_by': self.env.uid,
                'reviewed_at': fields.Datetime.now(),
            })
            rec._learn(origin='rejected', weight=0.5)
        return True

    def action_create_quotation(self):
        """Create a Sale Order from an approved rate_quote or load_tender draft."""
        self.ensure_one()
        if self.negotiation_id:
            return self.negotiation_id.create_quotation()

        # Standalone rate_quote → create a basic sale.order from context snapshot
        import json as _json
        try:
            ctx = _json.loads(self.context_snapshot or '{}')
        except Exception:
            ctx = {}

        partner = None
        lead = self.env['crm.lead']
        if self.source_model == 'crm.lead' and self.source_id:
            lead = self.env['crm.lead'].browse(self.source_id)
            if lead.exists():
                partner = lead.partner_id or None
        elif self.source_model == 'discuss.channel' and self.source_id:
            ch = self.env['discuss.channel'].browse(self.source_id)
            if ch.exists():
                partner = ch.whatsapp_partner_id or None

        if not partner:
            raise exceptions.UserError(
                'Cannot determine the customer for this draft. '
                'Link it to a CRM lead or WhatsApp channel first, or create the quotation manually.')

        product = self.env['product.product'].sudo().search([
            ('type', '=', 'service'),
            '|', ('name', 'ilike', 'freight'), ('name', 'ilike', 'transport'),
        ], limit=1)

        order_vals = {
            'partner_id': partner.id,
            'note': self.human_final or self.ai_suggestion,
        }
        if lead:
            # Link the originating CRM lead so the draft quotation appears on the
            # lead and the lead is never left with an orphan quote (no stage move
            # here — Studio rules stay in charge of lead stages).
            order_vals['opportunity_id'] = lead.id
        if product:
            order_vals['order_line'] = [(0, 0, {
                'product_id':      product.id,
                'name':            f'Freight Service — {partner.name}',
                'product_uom_qty': 1,
                'price_unit':      0.0,
            })]

        order = self.env['sale.order'].sudo().create(order_vals)
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id':    order.id,
            'view_mode': 'form',
            'target':    'current',
        }

    def action_open_source(self):
        """Jump to the originating record."""
        self.ensure_one()
        if not self.source_model or not self.source_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.source_model,
            'res_id': self.source_id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_apply_to_bill(self):
        """
        Apply the extracted bill data to the linked vendor bill in draft state.
        Sets header fields (vendor, ref, date) and replaces product lines with
        the AI-extracted lines + suggested account codes.
        Feeds approval into the knowledge base so the same vendor is faster next time.
        """
        self.ensure_one()
        if self.draft_type != 'bill_autofill':
            raise exceptions.UserError('This action is only available for Bill Auto-Fill drafts.')

        try:
            data = _json.loads(self.human_final or self.ai_suggestion)
        except Exception:
            raise exceptions.UserError(
                'The suggestion content is not valid JSON. '
                'Edit the Final Version field with correct JSON before applying.')

        if not self.source_model or not self.source_id:
            raise exceptions.UserError('No source bill is linked to this draft.')

        invoice = self.env['account.move'].browse(self.source_id)
        if not invoice.exists():
            raise exceptions.UserError('The linked vendor bill no longer exists.')
        if invoice.state != 'draft':
            raise exceptions.UserError(
                'The linked bill is already posted. Only draft bills can be auto-filled.')

        header_vals = {}

        # Vendor / partner
        if data.get('vendor_name') and not invoice.partner_id:
            partner = self.env['res.partner'].search([
                ('name', 'ilike', data['vendor_name']),
            ], limit=1)
            if partner:
                header_vals['partner_id'] = partner.id

        # Reference / invoice number
        if data.get('invoice_number') and not invoice.ref:
            header_vals['ref'] = data['invoice_number']

        # Invoice date
        if data.get('invoice_date') and not invoice.invoice_date:
            try:
                from datetime import date
                header_vals['invoice_date'] = data['invoice_date']
            except Exception:
                pass

        # Currency
        if data.get('currency') and data['currency'] not in ('', 'CAD'):
            currency = self.env['res.currency'].search(
                [('name', '=', data['currency'])], limit=1)
            if currency:
                header_vals['currency_id'] = currency.id

        if header_vals:
            invoice.write(header_vals)

        # Replace product lines
        line_items = data.get('line_items') or []
        if line_items:
            existing_product_lines = invoice.invoice_line_ids.filtered(
                lambda l: l.display_type == 'product')
            existing_product_lines.unlink()

            for item in line_items[:25]:
                desc = (item.get('description') or 'Service').strip()
                if not desc:
                    continue
                qty = float(item.get('quantity') or 1) or 1
                amount = float(item.get('amount') or 0)
                unit_price = float(item.get('unit_price') or 0) or (amount / qty if qty else 0)

                acc_code = (item.get('suggested_account_code') or '').strip()
                account = None
                if acc_code:
                    account = self.env['account.account'].search([
                        ('code', '=like', f'{acc_code}%'),
                        ('company_ids', 'in', invoice.company_id.id),
                    ], limit=1)
                # Fallback: use the journal's default expense account
                if not account and invoice.journal_id:
                    account = invoice.journal_id.default_account_id

                tax_code = (item.get('tax_code') or '').strip()
                tax = self.env['account.tax'].search([
                    ('name', 'ilike', tax_code),
                    ('type_tax_use', '=', 'purchase'),
                    ('company_id', '=', invoice.company_id.id),
                ], limit=1) if tax_code else self.env['account.tax']

                line_vals = {
                    'move_id': invoice.id,
                    'name': desc,
                    'quantity': qty,
                    'price_unit': unit_price,
                    'display_type': 'product',
                }
                if account:
                    line_vals['account_id'] = account.id
                if tax:
                    line_vals['tax_ids'] = [(4, tax.id)]

                try:
                    self.env['account.move.line'].create(line_vals)
                except Exception as e:
                    _logger.warning('Bill autofill: could not create line "%s": %s', desc, e)

        # Record the approval + learn
        self.write({
            'status': 'approved',
            'reviewed_by': self.env.uid,
            'reviewed_at': fields.Datetime.now(),
        })
        self._learn(origin='approved', weight=2.0)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_apply_to_order(self):
        """
        Apply extracted rate confirmation data to the linked sale order (draft state).
        Sets customer reference, order note, and replaces lines with the agreed freight rate.
        """
        self.ensure_one()
        if self.draft_type != 'rate_conf_autofill':
            raise exceptions.UserError('This action is only available for Rate Conf Auto-Fill drafts.')

        try:
            data = _json.loads(self.human_final or self.ai_suggestion)
        except Exception:
            raise exceptions.UserError(
                'The suggestion content is not valid JSON. '
                'Edit the Final Version field with correct JSON before applying.')

        if not self.source_model or not self.source_id:
            raise exceptions.UserError('No source sale order is linked to this draft.')

        order = self.env['sale.order'].browse(self.source_id)
        if not order.exists():
            raise exceptions.UserError('The linked sale order no longer exists.')
        if order.state not in ('draft', 'sent'):
            raise exceptions.UserError(
                'The linked order is already confirmed. Only draft quotations can be auto-filled.')

        header_vals = {}

        # Customer reference (rate conf #)
        if data.get('rate_conf_number') and not order.client_order_ref:
            header_vals['client_order_ref'] = data['rate_conf_number']

        # Internal note with stop summary
        deliveries = data.get('delivery_locations') or []
        stops_line = ' → '.join(deliveries) if deliveries else ''
        note_parts = []
        if data.get('pickup_location'):
            note_parts.append(f"Pickup: {data['pickup_location']}")
        if stops_line:
            note_parts.append(f"Deliver to: {stops_line}")
        if data.get('special_instructions'):
            note_parts.append(f"Notes: {data['special_instructions']}")
        if note_parts:
            header_vals['note'] = '\n'.join(note_parts)

        if header_vals:
            order.write(header_vals)

        # Replace order lines with the agreed freight charge
        total_rate = float(data.get('total_rate') or 0)
        if total_rate > 0:
            existing = order.order_line.filtered(lambda l: l.display_type == 'line_section' or l.product_id)
            existing.unlink()

            product = self.env['product.product'].sudo().search([
                ('type', '=', 'service'),
                '|', ('name', 'ilike', 'freight'), ('name', 'ilike', 'transport'),
            ], limit=1)

            service_label = data.get('service_type') or 'Freight Service'
            if data.get('pickup_date'):
                service_label += f" — {data['pickup_date']}"

            self.env['sale.order.line'].create({
                'order_id':        order.id,
                'name':            service_label,
                'product_id':      product.id if product else False,
                'product_uom_qty': 1,
                'price_unit':      total_rate,
            })

        self.write({
            'status': 'approved',
            'reviewed_by': self.env.uid,
            'reviewed_at': fields.Datetime.now(),
        })
        self._learn(origin='approved', weight=2.0)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.depends('ai_suggestion', 'draft_type')
    def _compute_ai_suggestion_display(self):
        for rec in self:
            raw = rec.ai_suggestion or ""
            if rec.draft_type == 'bill_autofill' and (raw.strip().startswith('{') or raw.strip().startswith('[')):
                try:
                    data = _json.loads(raw)
                    lines = []
                    if data.get('vendor_name'):
                        lines.append(f"Vendor: {data['vendor_name']}")
                    if data.get('invoice_number'):
                        lines.append(f"Invoice #: {data['invoice_number']}")
                    if data.get('invoice_date'):
                        lines.append(f"Date: {data['invoice_date']}")
                    if data.get('total_amount'):
                        lines.append(f"Total: {data['total_amount']} {data.get('currency', 'CAD')}")
                    if data.get('tax_amount'):
                        lines.append(f"Tax: {data['tax_amount']}")
                    items = data.get('line_items') or []
                    if items:
                        lines.append("Line Items:")
                        for item in items:
                            lines.append(
                                f"  - {item.get('description', '')}  "
                                f"qty: {item.get('quantity', '')}  "
                                f"price: {item.get('unit_price', '')}  "
                                f"account: {item.get('suggested_account_code', '')}"
                            )
                    rec.ai_suggestion_display = "\n".join(lines) or raw
                except Exception:
                    rec.ai_suggestion_display = raw
            elif rec.draft_type == 'rate_conf_autofill' and (raw.strip().startswith('{') or raw.strip().startswith('[')):
                try:
                    data = _json.loads(raw)
                    lines = []
                    if data.get('rate_conf_number'):
                        lines.append(f"Rate Conf #: {data['rate_conf_number']}")
                    if data.get('customer_name'):
                        lines.append(f"Customer: {data['customer_name']}")
                    if data.get('total_rate'):
                        lines.append(f"Rate: ${data['total_rate']} {data.get('currency', 'CAD')}")
                    if data.get('pickup_date'):
                        lines.append(f"Pickup Date: {data['pickup_date']}")
                    if data.get('pickup_location'):
                        lines.append(f"Pickup: {data['pickup_location']}")
                    deliveries = data.get('delivery_locations') or []
                    if deliveries:
                        lines.append(f"Deliver to: {' → '.join(deliveries)}")
                    if data.get('special_instructions'):
                        lines.append(f"Notes: {data['special_instructions']}")
                    rec.ai_suggestion_display = "\n".join(lines) or raw
                except Exception:
                    rec.ai_suggestion_display = raw
            else:
                rec.ai_suggestion_display = raw

    # ── Learning ───────────────────────────────────────────────────

    def _learn(self, origin, weight):
        """Push this draft into the knowledge base."""
        self.ensure_one()
        good_output = self.human_final or self.ai_suggestion
        self.env['premafirm.ml.knowledge'].create({
            'knowledge_type':  self.draft_type,
            'input_context':   self.context_snapshot or '',
            'good_output':     good_output,
            'correction_note': self.feedback_note or '',
            'source_draft_id': self.id,
            'origin':          origin,
            'weight':          weight,
        })
