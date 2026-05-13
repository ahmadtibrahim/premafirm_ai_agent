"""
Bulk historical data ingestion into the ML knowledge base.
Scans existing Odoo records and seeds the knowledge base so
the AI starts with context from day one instead of learning from scratch.

Safe to run multiple times — skips records already ingested
by checking source_draft_id IS NULL and origin = 'ingested'.
"""
import re
from odoo import api, fields, models


class MLIngestion(models.Model):
    _name = 'premafirm.ml.ingestion'
    _description = 'ML Bulk Ingestion Job'
    _order = 'create_date desc'

    name = fields.Char(default='Ingestion Run', readonly=True)
    state = fields.Selection([
        ('draft', 'Ready'),
        ('running', 'Running'),
        ('done', 'Done'),
    ], default='draft')
    create_date = fields.Datetime(readonly=True)

    # Counters
    sale_orders_done        = fields.Integer('Sale Orders', readonly=True)
    invoices_done           = fields.Integer('Invoices', readonly=True)
    vendor_bills_done       = fields.Integer('Vendor Bills', readonly=True)
    customer_invoices_done  = fields.Integer('Customer Invoices', readonly=True)
    whatsapp_done           = fields.Integer('WA Conversations', readonly=True)
    partners_done           = fields.Integer('Tagged Contacts', readonly=True)
    crm_done                = fields.Integer('CRM Messages', readonly=True)
    total_done              = fields.Integer('Total Entries', compute='_compute_total', store=True)
    log = fields.Text('Log', readonly=True)

    @api.depends('sale_orders_done', 'invoices_done', 'vendor_bills_done',
                 'customer_invoices_done', 'whatsapp_done', 'partners_done', 'crm_done')
    def _compute_total(self):
        for r in self:
            r.total_done = (r.sale_orders_done + r.invoices_done +
                            r.vendor_bills_done + r.customer_invoices_done +
                            r.whatsapp_done + r.partners_done + r.crm_done)

    # ── Entry point ────────────────────────────────────────────────

    def action_run(self):
        self.ensure_one()
        self.write({'state': 'running', 'log': 'Starting ingestion...\n'})
        logs = []
        totals = {}

        totals['sale_orders_done']       = self._ingest_sale_orders(logs)
        totals['invoices_done']          = self._ingest_invoices(logs)
        totals['vendor_bills_done']      = self._ingest_vendor_bills(logs)
        totals['customer_invoices_done'] = self._ingest_customer_invoices(logs)
        totals['whatsapp_done']          = self._ingest_whatsapp(logs)
        totals['partners_done']          = self._ingest_tagged_partners(logs)
        totals['crm_done']               = self._ingest_crm_messages(logs)

        # Zero-cost bonus ingest: WA negotiations + purchase orders + fleet + contacts
        neg_count     = self._ingest_wa_negotiations(logs)
        po_count      = self._ingest_purchase_orders(logs)
        fleet_count   = self._ingest_fleet_vehicles(logs)
        contact_count = self._ingest_contact_profiles(logs)
        totals['sale_orders_done'] += neg_count + po_count
        totals['partners_done']    += fleet_count + contact_count

        # Retrain local scikit-learn models in prema_ai_auditor
        self._retrain_local_ml_models(logs)

        self.write({
            'state': 'done',
            'log': '\n'.join(logs),
            **totals,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'ML Ingestion Complete',
                'message': (
                    f"{totals['sale_orders_done']} orders/negotiations/POs, "
                    f"{totals['invoices_done']} anomaly baselines, "
                    f"{totals['vendor_bills_done']} vendor bills, "
                    f"{totals['customer_invoices_done']} customer invoices, "
                    f"{totals['whatsapp_done']} WA conversations, "
                    f"{totals['partners_done']} contacts/fleet, "
                    f"{totals['crm_done']} CRM messages ingested."
                ),
                'type': 'success',
                'sticky': True,
            },
        }

    def action_ingest_vendor_bills_only(self):
        """Quick action to ingest only vendor bills — useful after entering new bills."""
        self.ensure_one()
        logs = []
        count = self._ingest_vendor_bills(logs)
        self.write({
            'vendor_bills_done': count,
            'log': (self.log or '') + '\n' + '\n'.join(logs),
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Vendor Bills Ingested',
                'message': f"{count} vendor bill pattern(s) added to ML knowledge base.",
                'type': 'success',
                'sticky': False,
            },
        }

    # ── Sale orders (rate quote learning) ─────────────────────────

    def _ingest_sale_orders(self, logs):
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'rate_quote'), ('origin', '=', 'ingested')])}

        orders = self.env['sale.order'].search(
            [('state', 'in', ['sale', 'done'])],
            order='date_order desc', limit=300,
        )
        count = 0
        for order in orders:
            lines = '\n'.join(
                f"  {l.name or l.product_id.name or 'Line'}: "
                f"{l.product_uom_qty} × {l.price_unit} {order.currency_id.name}"
                for l in order.order_line[:10]
            )
            context = (
                f"Customer: {order.partner_id.name}\n"
                f"Date: {order.date_order.strftime('%Y-%m-%d') if order.date_order else ''}\n"
                f"Reference: {order.name}\n"
                f"Lines:\n{lines}"
            )
            # Also pull attachment text
            att = self._quick_attachment_text('sale.order', order.id)
            if att:
                context += f'\nAttachment:\n{att[:800]}'

            if context[:60] in already:
                continue

            good_output = (
                f"Total: {order.amount_total} {order.currency_id.name}\n"
                f"Payment terms: {order.payment_term_id.name if order.payment_term_id else 'Standard'}"
            )
            KB.create({
                'knowledge_type': 'rate_quote',
                'input_context':  context,
                'good_output':    good_output,
                'origin':         'ingested',
                'weight':         1.0,
            })
            count += 1

        logs.append(f'Sale orders ingested: {count}')
        return count

    # ── Invoices (baseline for anomaly detection) ──────────────────

    def _ingest_invoices(self, logs):
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'invoice_flag'), ('origin', '=', 'ingested')])}

        invoices = self.env['account.move'].search([
            ('state', '=', 'posted'),
            ('move_type', 'in', ['in_invoice', 'out_invoice']),
        ], order='invoice_date desc', limit=300)

        count = 0
        for inv in invoices:
            lines = '\n'.join(
                f"  {l.name or l.product_id.name or 'Line'}: "
                f"{l.quantity} × {l.price_unit} {inv.currency_id.name}"
                for l in inv.invoice_line_ids[:8]
            )
            context = (
                f"Vendor/Customer: {inv.partner_id.name if inv.partner_id else 'Unknown'}\n"
                f"Amount: {inv.amount_total} {inv.currency_id.name}\n"
                f"Date: {inv.invoice_date}\n"
                f"Type: {inv.move_type}\n"
                f"Journal: {inv.journal_id.name if inv.journal_id else ''}\n"
                f"Lines:\n{lines}"
            )
            if context[:60] in already:
                continue

            KB.create({
                'knowledge_type': 'invoice_flag',
                'input_context':  context,
                'good_output':    'Normal — posted and approved invoice.',
                'origin':         'ingested',
                'weight':         0.8,
            })
            count += 1

        logs.append(f'Invoices ingested: {count}')
        return count

    # ── Vendor bills (bill_import pattern learning) ────────────────

    def _ingest_vendor_bills(self, logs):
        """
        Ingest all posted vendor bills into the ML knowledge base as bill_import entries.
        For each bill, stores: vendor name, account codes used, line descriptions.
        The bill scan service then uses these patterns to pre-fill accounts on new scans.
        """
        import json
        KB = self.env['premafirm.ml.knowledge']

        # Deduplicate by vendor name + invoice ref
        already = {
            e.input_context[:80]
            for e in KB.search([
                ('knowledge_type', '=', 'bill_import'),
                ('origin', '=', 'ingested'),
            ])
        }

        bills = self.env['account.move'].search([
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'posted'),
            ('partner_id', '!=', False),
        ], order='invoice_date desc', limit=500)

        count = 0
        for bill in bills:
            # Use parent company name for multi-location vendors
            vendor_name    = (
                bill.partner_id.parent_id.name if bill.partner_id.parent_id
                else bill.partner_id.name
            ) or 'Unknown'
            invoice_number = bill.ref or bill.name or ''
            lines_data     = []
            account_codes  = []
            product_names  = []

            for ln in bill.invoice_line_ids:
                if not ln.account_id or ln.display_type not in ('product', False):
                    continue
                prod_name = ln.product_id.name if ln.product_id else None
                tax_names = [t.name for t in ln.tax_ids]
                lines_data.append({
                    'description':   (ln.name or '')[:80],
                    'account_code':  ln.account_id.code,
                    'account_name':  ln.account_id.name,
                    'product_id':    ln.product_id.id if ln.product_id else None,
                    'product_name':  prod_name,
                    'tax_names':     tax_names,
                })
                if ln.account_id.code not in account_codes:
                    account_codes.append(ln.account_id.code)
                if prod_name and prod_name not in product_names:
                    product_names.append(prod_name)

            if not lines_data:
                continue

            input_ctx = (
                f"Vendor: {vendor_name} | "
                f"Invoice: {invoice_number} | "
                f"Total: {bill.amount_total} {bill.currency_id.name} | "
                f"Accounts: {', '.join(account_codes)}"
                + (f" | Products: {', '.join(product_names)}" if product_names else '')
            )
            key = input_ctx[:80]

            good_output = json.dumps({
                'vendor_name':    vendor_name,
                'partner_id':     bill.partner_id.id,
                'partner_name':   bill.partner_id.name,
                'invoice_number': invoice_number,
                'invoice_date':   str(bill.invoice_date or ''),
                'total':          bill.amount_total,
                'currency':       bill.currency_id.name,
                'account_codes':  account_codes,
                'product_names':  product_names,
                'lines':          lines_data,
            }, indent=2)

            if key in already:
                # Update existing entry to add product info
                existing_entry = KB.search([
                    ('knowledge_type', '=', 'bill_import'),
                    ('origin', '=', 'ingested'),
                    ('input_context', 'like', key),
                ], limit=1)
                if existing_entry and '"product_id": null' in existing_entry.good_output:
                    existing_entry.write({'good_output': good_output, 'input_context': input_ctx})
                continue

            KB.create({
                'knowledge_type': 'bill_import',
                'input_context':  input_ctx,
                'good_output':    good_output,
                'origin':         'ingested',
                'weight':         1.0,
            })
            already.add(key)
            count += 1

        # Build/refresh vendor profiles after individual bill ingestion
        profile_count = self._build_vendor_profiles(KB, bills)
        logs.append(f'Vendor bills ingested: {count}, vendor profiles updated: {profile_count}')
        return count

    def _build_vendor_profiles(self, KB, bills):
        """
        Build one high-weight KB entry per vendor summarising their typical
        product, account code, and tax from all posted bills.

        This is the key that lets the AI say "Princess Auto → always Tools & Equipment"
        even when the receipt only has a free-text description and no product selected.
        Weight 3.0 so it outranks individual bill examples in few-shot selection.
        """
        import json
        from collections import defaultdict, Counter

        vendor_stats = defaultdict(lambda: {
            'products': Counter(),
            'accounts': Counter(),
            'taxes':    Counter(),
            'partner_id': None,
        })

        for bill in bills:
            vname = (
                bill.partner_id.parent_id.name if bill.partner_id.parent_id
                else bill.partner_id.name
            ) or ''
            if not vname:
                continue
            vendor_stats[vname]['partner_id'] = bill.partner_id.id
            for ln in bill.invoice_line_ids:
                if ln.display_type != 'product' or not ln.account_id:
                    continue
                if ln.product_id:
                    vendor_stats[vname]['products'][ln.product_id.name] += 1
                vendor_stats[vname]['accounts'][ln.account_id.code] += 1
                for t in ln.tax_ids:
                    vendor_stats[vname]['taxes'][t.name] += 1

        count = 0
        for vname, stats in vendor_stats.items():
            if not stats['accounts']:
                continue

            top_products = stats['products'].most_common(5)
            top_accounts = stats['accounts'].most_common(3)
            top_taxes    = stats['taxes'].most_common(1)

            profile = {
                'vendor_name':     vname,
                'default_product': top_products[0][0] if top_products else None,
                'default_account': top_accounts[0][0] if top_accounts else None,
                'default_tax':     top_taxes[0][0] if top_taxes else None,
                'top_products': [{'name': p, 'count': c} for p, c in top_products],
                'top_accounts': [{'code': a, 'count': c} for a, c in top_accounts],
            }
            ctx = f'VENDOR_PROFILE: {vname}'
            existing = KB.search([
                ('knowledge_type', '=', 'bill_import'),
                ('input_context', '=', ctx),
            ], limit=1)
            if existing:
                existing.write({
                    'good_output': json.dumps(profile, indent=2),
                    'weight': 3.0,
                })
            else:
                KB.create({
                    'knowledge_type': 'bill_import',
                    'input_context':  ctx,
                    'good_output':    json.dumps(profile, indent=2),
                    'origin':         'vendor_profile',
                    'weight':         3.0,
                })
                count += 1

        return count

    # ── WhatsApp conversations (WA reply learning) ─────────────────

    def _ingest_whatsapp(self, logs):
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'wa_reply'), ('origin', '=', 'ingested')])}

        channels = self.env['discuss.channel'].search(
            [('channel_type', '=', 'whatsapp')], limit=100)

        count = 0
        for ch in channels:
            messages = self.env['mail.message'].search([
                ('res_id', '=', ch.id),
                ('model', '=', 'discuss.channel'),
                ('message_type', 'in', ['comment', 'whatsapp_message']),
            ], order='date asc', limit=40)

            # Extract request→reply pairs
            msgs = list(messages)
            for i, msg in enumerate(msgs[:-1]):
                body = re.sub(r'<[^>]+>', ' ', msg.body or '').strip()
                if not body:
                    continue
                # Look for the next internal user reply
                reply_msg = None
                for j in range(i + 1, min(i + 4, len(msgs))):
                    if msgs[j].author_id and msgs[j].author_id.user_ids:
                        reply_body = re.sub(r'<[^>]+>', ' ', msgs[j].body or '').strip()
                        if reply_body:
                            reply_msg = reply_body
                            break
                if not reply_msg:
                    continue

                partner = ch.whatsapp_partner_id
                context = (
                    f"Contact: {partner.name if partner else 'Customer'}\n"
                    f"Message: {body[:500]}"
                )
                if context[:60] in already:
                    continue

                KB.create({
                    'knowledge_type': 'wa_reply',
                    'input_context':  context,
                    'good_output':    reply_msg[:800],
                    'origin':         'ingested',
                    'weight':         1.2,
                })
                count += 1

        logs.append(f'WA conversations ingested: {count}')
        return count

    # ── Tagged partners (customer tag learning) ────────────────────

    def _ingest_tagged_partners(self, logs):
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'customer_tag'), ('origin', '=', 'ingested')])}

        partners = self.env['res.partner'].search(
            [('category_id', '!=', False), ('active', '=', True)], limit=300)

        count = 0
        for p in partners:
            tags = ', '.join(p.category_id.mapped('name'))
            if not tags:
                continue
            context = (
                f"Contact: {p.name}\n"
                f"Company: {p.parent_id.name if p.parent_id else 'Individual'}\n"
                f"Country: {p.country_id.name if p.country_id else ''}"
            )
            if context[:60] in already:
                continue

            KB.create({
                'knowledge_type': 'customer_tag',
                'input_context':  context,
                'good_output':    tags,
                'origin':         'ingested',
                'weight':         1.0,
            })
            count += 1

        logs.append(f'Tagged contacts ingested: {count}')
        return count

    # ── CRM email messages (CRM reply learning) ────────────────────

    def _ingest_crm_messages(self, logs):
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'crm_reply'), ('origin', '=', 'ingested')])}

        leads = self.env['crm.lead'].search(
            [('stage_id.is_won', '=', True)], limit=100)

        count = 0
        for lead in leads:
            messages = self.env['mail.message'].search([
                ('res_id', '=', lead.id),
                ('model', '=', 'crm.lead'),
                ('message_type', 'in', ['email', 'comment']),
            ], order='date asc', limit=20)

            msgs = list(messages)
            for i, msg in enumerate(msgs[:-1]):
                # External message
                if msg.author_id and msg.author_id.user_ids:
                    continue
                body = re.sub(r'<[^>]+>', ' ', msg.body or '').strip()[:400]
                if not body:
                    continue
                # Find next internal reply
                for j in range(i + 1, min(i + 3, len(msgs))):
                    if msgs[j].author_id and msgs[j].author_id.user_ids:
                        reply = re.sub(r'<[^>]+>', ' ', msgs[j].body or '').strip()[:600]
                        if not reply:
                            continue
                        context = (
                            f"Lead: {lead.name}\n"
                            f"Company: {lead.partner_name or ''}\n"
                            f"Message: {body}"
                        )
                        if context[:60] in already:
                            continue
                        KB.create({
                            'knowledge_type': 'crm_reply',
                            'input_context':  context,
                            'good_output':    reply,
                            'origin':         'ingested',
                            'weight':         1.2,
                        })
                        count += 1
                        break

        logs.append(f'CRM messages ingested: {count}')
        return count

    # ── WA Negotiations (agreed/completed deals → rate_quote learning) ──

    def _ingest_wa_negotiations(self, logs):
        """Ingest completed WA negotiations as rate_quote knowledge entries.

        Each agreed negotiation contains real routing, commodity, equipment type,
        and the agreed rate — exactly the data the AI needs to quote accurately.
        No API calls — pure structured DB data.
        """
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:80] for e in KB.search(
            [('knowledge_type', '=', 'rate_quote'),
             ('origin', '=', 'negotiation')])}

        negotiations = self.env['premafirm.wa.negotiation'].sudo().search([
            ('status', 'in', ('agreed', 'rate_conf_sent', 'completed')),
            ('agreed_rate', '>', 0),
        ], limit=500)

        count = 0
        for neg in negotiations:
            stops = neg.stop_ids.sorted('sequence')
            if not stops:
                continue

            route_parts = [
                f"{s.stop_type.upper()}: {', '.join(p for p in [s.company_name, s.street, s.city, s.province] if p)}"
                for s in stops
            ]
            route = ' → '.join(route_parts)

            context = (
                f"Partner: {neg.partner_id.name if neg.partner_id else 'Unknown'}\n"
                f"Equipment: {neg.equipment_type or 'N/A'} | "
                f"Service: {neg.service_type or 'N/A'} | "
                f"Commodity: {neg.commodity or 'N/A'} | "
                f"Weight: {neg.total_weight_lbs or 0} lbs\n"
                f"Route: {route}"
            )
            if context[:80] in already:
                continue

            output_lines = [f"Agreed rate: ${neg.agreed_rate:,.2f} CAD"]
            if neg.estimated_cost:
                output_lines.append(f"Estimated cost: ${neg.estimated_cost:,.2f}")
            if neg.our_counter:
                output_lines.append(f"Our counter: ${neg.our_counter:,.2f}")
            good_output = '\n'.join(output_lines)

            KB.create({
                'knowledge_type': 'rate_quote',
                'input_context':  context,
                'good_output':    good_output,
                'origin':         'negotiation',
                'weight':         1.5,
            })
            already.add(context[:80])
            count += 1

        logs.append(f'WA negotiations ingested: {count}')
        return count

    # ── Purchase Orders (vendor pattern learning) ──────────────────

    def _ingest_purchase_orders(self, logs):
        """Ingest confirmed purchase orders as vendor/supply knowledge.

        Gives the AI context on what PremaFirm buys, from whom, and at what price
        ranges — useful for anomaly detection on vendor bills and rate suggestions.
        No API calls.
        """
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'bill_import'),
             ('origin', '=', 'purchase')])}

        orders = self.env['purchase.order'].sudo().search([
            ('state', 'in', ('purchase', 'done')),
        ], order='date_order desc', limit=300)

        count = 0
        for order in orders:
            lines = '\n'.join(
                f"  {l.name or l.product_id.name or 'Line'}: "
                f"{l.product_qty} × {l.price_unit} {order.currency_id.name}"
                for l in order.order_line[:10]
            )
            context = (
                f"Vendor: {order.partner_id.name}\n"
                f"Date: {order.date_order.strftime('%Y-%m-%d') if order.date_order else ''}\n"
                f"Reference: {order.name}\n"
                f"Lines:\n{lines}"
            )
            if context[:60] in already:
                continue

            KB.create({
                'knowledge_type': 'bill_import',
                'input_context':  context,
                'good_output':    (
                    f"Total: {order.amount_total} {order.currency_id.name}\n"
                    f"Payment terms: {order.payment_term_id.name if order.payment_term_id else 'Standard'}"
                ),
                'origin':         'purchase',
                'weight':         0.9,
            })
            already.add(context[:60])
            count += 1

        logs.append(f'Purchase orders ingested: {count}')
        return count

    # ── Fleet vehicles (dispatch context learning) ─────────────────

    def _ingest_fleet_vehicles(self, logs):
        """Ingest active fleet vehicles as rate_quote context.

        Gives the AI knowledge of available truck types, capacities, and
        capabilities — used when generating rate quotes and routing suggestions.
        No API calls.
        """
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'rate_quote'),
             ('origin', '=', 'fleet')])}

        vehicles = self.env['fleet.vehicle'].sudo().search([('active', '=', True)])
        count = 0
        for v in vehicles:
            brand    = v.model_id.brand_id.name if (v.model_id and v.model_id.brand_id) else ''
            model_nm = v.model_id.name if v.model_id else ''
            is_reefer   = getattr(v, 'x_reefer', False)
            has_liftgate = getattr(v, 'x_liftgate', False)
            max_payload = getattr(v, 'x_max_payload_lbs', 0) or 0
            max_pallets = getattr(v, 'x_max_pallets', 0) or 0
            tank_cap    = getattr(v, 'x_tank_capacity_l', 0) or 0

            context = (
                f"Truck: {v.license_plate or v.name} | "
                f"Make/Model: {brand} {model_nm} | "
                f"Type: {'Reefer' if is_reefer else 'Dry Van'} | "
                f"Max payload: {int(max_payload)} lbs | "
                f"Max pallets: {int(max_pallets)} | "
                f"Liftgate: {'Yes' if has_liftgate else 'No'} | "
                f"Tank: {int(tank_cap)} L"
            )
            if context[:60] in already:
                continue

            KB.create({
                'knowledge_type': 'rate_quote',
                'input_context':  context,
                'good_output':    (
                    f"Active truck: {v.license_plate or v.name}. "
                    f"{'Reefer capable. ' if is_reefer else ''}"
                    f"{'Liftgate available. ' if has_liftgate else ''}"
                    f"Max {int(max_payload)} lbs / {int(max_pallets)} pallets."
                ),
                'origin':  'fleet',
                'weight':  0.5,
            })
            already.add(context[:60])
            count += 1

        logs.append(f'Fleet vehicles ingested: {count}')
        return count

    # ── Contact profiles (CRM + tagging context learning) ──────────

    def _ingest_contact_profiles(self, logs):
        """Ingest active company contacts with their business context.

        Gives the AI knowledge of who PremaFirm works with — used when
        drafting CRM replies, tagging contacts, and recognizing partners.
        Ingests companies that have at least one invoice, sale, or CRM record.
        No API calls.
        """
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:60] for e in KB.search(
            [('knowledge_type', '=', 'customer_tag'),
             ('origin', '=', 'ingested')])}

        self.env.cr.execute("""
            SELECT DISTINCT rp.id
            FROM res_partner rp
            WHERE rp.is_company = true AND rp.active = true
              AND (
                  EXISTS (SELECT 1 FROM sale_order so WHERE so.partner_id = rp.id LIMIT 1)
                  OR EXISTS (SELECT 1 FROM account_move am WHERE am.partner_id = rp.id
                             AND am.move_type IN ('out_invoice','in_invoice') LIMIT 1)
                  OR EXISTS (SELECT 1 FROM crm_lead cl WHERE cl.partner_id = rp.id LIMIT 1)
              )
            ORDER BY rp.id
            LIMIT 500
        """)
        partner_ids = [r[0] for r in self.env.cr.fetchall()]
        partners = self.env['res.partner'].sudo().browse(partner_ids)

        count = 0
        for p in partners:
            tags = ', '.join(p.category_id.mapped('name')) or ''
            context = (
                f"Contact: {p.name} | "
                f"Country: {p.country_id.name if p.country_id else 'CA'} | "
                f"City: {p.city or ''} | "
                f"Province: {p.state_id.code if p.state_id else ''}"
            )
            if context[:60] in already:
                continue

            good_output = tags or 'No tags yet'
            KB.create({
                'knowledge_type': 'customer_tag',
                'input_context':  context,
                'good_output':    good_output,
                'origin':         'ingested',
                'weight':         0.8,
            })
            already.add(context[:60])
            count += 1

        logs.append(f'Contact profiles ingested: {count}')
        return count

    # ── Customer Invoices (auto-invoice suggestions) ───────────────

    def _ingest_customer_invoices(self, logs):
        """
        Ingest posted customer invoices as rate_quote knowledge.
        Teaches the AI what PremaFirm charges customers for freight services,
        enabling auto-invoice suggestions when creating new invoices.
        No API calls.
        """
        import json
        KB = self.env['premafirm.ml.knowledge']
        already = {e.input_context[:80] for e in KB.search(
            [('knowledge_type', '=', 'rate_quote'),
             ('origin', '=', 'invoice')])}

        invoices = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('partner_id', '!=', False),
        ], order='invoice_date desc', limit=500)

        count = 0
        for inv in invoices:
            lines_data = []
            for ln in inv.invoice_line_ids:
                if ln.display_type != 'product':
                    continue
                lines_data.append({
                    'description': (ln.name or ln.product_id.name or '')[:80],
                    'quantity':    ln.quantity,
                    'unit_price':  ln.price_unit,
                    'account_code': ln.account_id.code if ln.account_id else '',
                })
            if not lines_data:
                continue

            context = (
                f"Customer: {inv.partner_id.name} | "
                f"Invoice: {inv.name} | "
                f"Total: {inv.amount_total} {inv.currency_id.name} | "
                f"Date: {inv.invoice_date}"
            )
            if context[:80] in already:
                continue

            KB.create({
                'knowledge_type': 'rate_quote',
                'input_context':  context,
                'good_output':    json.dumps({
                    'invoice_name':   inv.name,
                    'partner':        inv.partner_id.name,
                    'total':          inv.amount_total,
                    'currency':       inv.currency_id.name,
                    'lines':          lines_data,
                }, indent=2),
                'origin':  'invoice',
                'weight':  1.2,
            })
            already.add(context[:80])
            count += 1

        logs.append(f'Customer invoices ingested: {count}')
        return count

    # ── Local scikit-learn model retraining ────────────────────────

    def _retrain_local_ml_models(self, logs):
        """
        Retrain the scikit-learn models in prema_ai_auditor from current Odoo data.
        These models predict: account code, tax code, duplicate detection, field corrections.
        Zero API cost — runs entirely on-server.
        """
        try:
            from odoo.addons.prema_ai_auditor.services.ml.engine import (
                train_account_classifier,
                train_tax_predictor,
                train_duplicate_detector,
            )
            train_account_classifier(self.env)
            logs.append('Local ML: account classifier retrained.')
            train_tax_predictor(self.env)
            logs.append('Local ML: tax predictor retrained.')
            train_duplicate_detector(self.env)
            logs.append('Local ML: duplicate detector retrained.')
        except ImportError:
            logs.append('Local ML: prema_ai_auditor ML engine not found — skipped.')
        except Exception as e:
            logs.append(f'Local ML retrain error: {e}')

    # ── Helper ─────────────────────────────────────────────────────

    def _quick_attachment_text(self, model_name, record_id):
        atts = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', model_name),
            ('res_id', '=', record_id),
            ('type', '=', 'binary'),
        ], limit=2)
        for att in atts:
            if att.datas:
                try:
                    from odoo.addons.premafirm_ml.services import document_extractor
                    text, _m = document_extractor.extract_from_b64(
                        att.datas, att.mimetype or '', att.name or '')
                except Exception:
                    text = self.env['premafirm.ml.engine']._extract_file(
                        att.datas, att.mimetype or '', att.name or '')
                if text:
                    return text[:1500]
        return ''

    # ── Cron entry point ───────────────────────────────────────────

    @api.model
    def action_cron_incremental_ingest(self):
        """Called nightly to ingest new records added since last run."""
        job = self.create({'name': 'Nightly Incremental Ingest'})
        job.action_run()
