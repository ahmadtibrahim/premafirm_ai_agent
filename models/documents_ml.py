"""
Documents → Vendor Bill processing via AI.
Adds "Process with AI" to documents in the bills-entry folder:
  1. Extract text from PDF/image (free — Tesseract/pdfplumber, no vision API)
  2. Parse bill fields with GPT text model (cheap)
  3. Create draft vendor bill with extracted data
  4. Move the attachment to the bill (no duplication)
  5. Save pattern to ML knowledge base
  6. Move document to Processed Bills folder
"""
import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DocumentsML(models.Model):
    _inherit = 'documents.document'

    x_processed = fields.Boolean('AI Processed', default=False, copy=False,
                                  help='Set when this document has been successfully processed into a vendor bill.')
    x_bill_id = fields.Many2one('account.move', string='Created Bill',
                                 ondelete='set null', copy=False, readonly=True)
    x_process_error = fields.Char('Last Process Error', readonly=True, copy=False,
                                   help='Last error from AI bill extraction, if any.')

    # ── Main action ────────────────────────────────────────────────

    def action_process_with_ml(self):
        """
        Process selected binary documents through AI bill extraction.
        Skips already-processed documents and non-binary records.
        Creates a draft vendor bill for each document, then moves the
        document to the Processed Bills folder.
        """
        processed_folder = self.env.ref(
            'premafirm_ai_engine.documents_folder_processed_bills', raise_if_not_found=False)

        bills_created = []

        for doc in self:
            if doc.x_processed or doc.type != 'binary':
                continue

            attachment = doc.attachment_id
            if not attachment or not attachment.datas:
                doc.x_process_error = 'No file data found on this document.'
                continue

            # Step 1 & 2: free OCR + GPT text model (no vision API)
            try:
                draft, err = self.env['premafirm.ml.engine'].generate_bill_autofill(
                    attachment.datas,
                    attachment.mimetype or '',
                    attachment.name or '',
                )
            except Exception as e:
                _logger.exception('ML bill extraction failed for document %s', doc.name)
                doc.x_process_error = str(e)[:250]
                continue

            if err or not draft:
                msg = (err or 'Extraction returned no result.')[:250]
                doc.x_process_error = msg
                _logger.warning('Bill scan ML failed for %s: %s', doc.name, msg)
                continue

            # Step 3: parse extracted JSON
            try:
                data = json.loads(draft.ai_suggestion)
            except Exception:
                doc.x_process_error = 'ML response was not valid JSON.'
                continue

            # Step 4: find or create vendor partner (with multi-location support)
            partner, vendor_created = self._resolve_vendor(
                vendor_name=data.get('vendor_name', ''),
                station_address=data.get('station_address', ''),
                station_city=data.get('station_city', ''),
                station_province=data.get('station_province', ''),
                station_postal=data.get('station_postal_code', ''),
            )

            # Step 5: build bill lines
            bill_lines = self._build_bill_lines(data, attachment)
            if not bill_lines:
                doc.x_process_error = 'No bill lines could be built — check the document quality.'
                continue

            # Step 6: create draft vendor bill
            # Build station address note so the actual receipt address is visible on the bill
            station_parts = [
                data.get('station_name') or data.get('vendor_name') or '',
                data.get('station_address') or '',
                data.get('station_city') or '',
                data.get('station_province') or '',
                data.get('station_postal_code') or '',
            ]
            station_note = ', '.join(p.strip() for p in station_parts if p.strip())

            bill_vals = {
                'move_type':  'in_invoice',
                'partner_id': partner.id if partner else False,
                'ref':        (data.get('invoice_number') or '')[:64] or False,
                'invoice_line_ids': bill_lines,
                # bills-entry documents are receipts for already-paid purchases
                'invoice_payment_term_id': self.env.ref('account.account_payment_term_immediate').id,
            }
            if station_note:
                bill_vals['narration'] = f'Station: {station_note}'
            if data.get('invoice_date'):
                bill_vals['invoice_date'] = data['invoice_date']
            if data.get('due_date'):
                bill_vals['invoice_date_due'] = data['due_date']

            try:
                bill = self.env['account.move'].sudo().create(bill_vals)
            except Exception as e:
                _logger.exception('Bill creation failed for document %s', doc.name)
                doc.x_process_error = f'Bill creation error: {str(e)[:150]}'
                continue

            # Step 7: move attachment to the bill (no copy — same ir.attachment record)
            attachment.sudo().write({
                'res_model': 'account.move',
                'res_id':    bill.id,
            })

            # Step 8: mark draft as approved + feed ML knowledge base
            try:
                draft.sudo().write({
                    'source_model': 'account.move',
                    'source_id':    bill.id,
                    'status':       'approved',
                })
                draft.sudo()._learn(origin='approved', weight=1.5)
            except Exception:
                pass  # learning failure is non-fatal

            # Step 9: auto-create IFTA fuel log (non-fatal)
            try:
                # Re-extract OCR text for province detection fallback
                from odoo.addons.premafirm_ml.services import document_extractor as _de
                ocr_text, _ = _de.extract_from_b64(
                    attachment.datas, attachment.mimetype or '', attachment.name or '')
                self.env['premafirm.ifta.fuel.log'].sudo().create_from_bill_data(
                    bill, data, ocr_text=ocr_text or '')
            except Exception as e:
                _logger.warning('IFTA log creation failed (non-fatal): %s', e)

            # Step 10: mark document processed and move folder
            doc.write({
                'x_processed':     True,
                'x_bill_id':       bill.id,
                'x_process_error': False,
            })
            if processed_folder:
                doc.folder_id = processed_folder.id

            bills_created.append(bill.id)
            _logger.info('Bill scan: created bill %s from document %s', bill.name, doc.name)

        if not bills_created:
            return {
                'type': 'ir.actions.client',
                'tag':  'display_notification',
                'params': {
                    'title':   'No Bills Created',
                    'message': (
                        'No new documents were processed. '
                        'Documents may already be processed, or their attachments '
                        'could not be read. Check the "Last Process Error" column.'
                    ),
                    'type':    'warning',
                    'sticky':  False,
                },
            }

        action = self.env['ir.actions.act_window']._for_xml_id(
            'account.action_move_in_invoice_type')
        action['domain'] = [('id', 'in', bills_created)]
        action['name'] = f'Bills Created ({len(bills_created)})'
        return action

    # ── Helpers ────────────────────────────────────────────────────

    # Known fuel product IDs (product.product)
    _DIESEL_PRODUCT_ID = 292
    _DEF_PRODUCT_ID    = 293
    _FUEL_KEYWORDS     = ('FUEL', 'DIESEL', 'DSL', 'BIOD', 'GAS', 'PETRO', 'DEF', 'UNLEADED')

    def _resolve_vendor(self, vendor_name, station_address='', station_city='',
                        station_province='', station_postal=''):
        """
        Find or create the vendor partner with multi-location support.

        Logic:
          1. Search for an existing company partner matching the name.
          2. If found and a station address is provided:
               a. Look for a matching child contact at that city/street.
               b. If not found → create a new child contact (location) under the parent.
               c. Return the child so the bill shows the correct station address.
          3. If no parent found → auto-create the vendor as a new company with all
             available address details pre-filled (user can correct if needed).

        Returns (partner_record, was_newly_created).
        """
        name = (vendor_name or '').strip()
        if not name:
            return None, False

        Partner = self.env['res.partner'].sudo()

        # 1. Find existing parent company
        parent = Partner.search([
            ('name', 'ilike', name),
            ('active', '=', True),
            ('is_company', '=', True),
        ], limit=1)
        if not parent:
            # Fallback: any partner (contact or company) matching the name
            parent = Partner.search([
                ('name', 'ilike', name),
                ('active', '=', True),
                ('parent_id', '=', False),
            ], limit=1)

        if parent:
            # 2. Multi-location: check if station address differs from the parent's address
            has_station = bool(station_city or station_address)
            parent_city = (parent.city or '').lower()
            station_city_l = station_city.lower() if station_city else ''

            if has_station and station_city_l and station_city_l != parent_city:
                # Look for existing child at this city
                child = Partner.search([
                    ('parent_id', '=', parent.id),
                    ('city', 'ilike', station_city),
                    ('active', '=', True),
                ], limit=1)
                if not child and station_address:
                    child = Partner.search([
                        ('parent_id', '=', parent.id),
                        ('street', 'ilike', station_address[:20]),
                        ('active', '=', True),
                    ], limit=1)

                if not child:
                    # Create new child location under this vendor
                    child_vals = {
                        'parent_id': parent.id,
                        'name':      f'{parent.name} — {station_city or station_address}',
                        'type':      'other',
                        'street':    station_address or '',
                        'city':      station_city or '',
                        'zip':       station_postal or '',
                        'active':    True,
                        'is_company': False,
                    }
                    state, country = self._resolve_state_country(station_province, station_postal)
                    if state:
                        child_vals['state_id']   = state.id
                        child_vals['country_id'] = state.country_id.id
                    elif country:
                        child_vals['country_id'] = country.id
                    child = Partner.create(child_vals)
                    _logger.info('Bill scan: created new location "%s" under vendor "%s"',
                                 child.name, parent.name)

                return child, False

            return parent, False

        # 3. Auto-create new vendor with all available details
        new_vals = {
            'name':          name,
            'is_company':    True,
            'supplier_rank': 1,
            'active':        True,
            'street':        station_address or '',
            'city':          station_city or '',
            'zip':           station_postal or '',
        }
        state, country = self._resolve_state_country(station_province, station_postal)
        if state:
            new_vals['state_id']   = state.id
            new_vals['country_id'] = state.country_id.id
        elif country:
            new_vals['country_id'] = country.id
        else:
            # Default to Canada
            ca = self.env['res.country'].search([('code', '=', 'CA')], limit=1)
            if ca:
                new_vals['country_id'] = ca.id

        partner = Partner.create(new_vals)
        _logger.info('Bill scan: auto-created vendor "%s"', name)
        return partner, True

    def _resolve_state_country(self, province_hint, postal_code=''):
        """Return (res.country.state or None, res.country or None) from province name or postal code."""
        State   = self.env['res.country.state'].sudo()
        Country = self.env['res.country'].sudo()

        hint = (province_hint or '').strip()
        if hint:
            state = State.search([
                ('name', 'ilike', hint),
                ('country_id.code', 'in', ('CA', 'US')),
            ], limit=1)
            if state:
                return state, state.country_id

        pc = (postal_code or '').replace(' ', '').upper()
        if pc and pc[0].isalpha():
            # Canadian postal → province first letter mapping
            _map = {
                'A': 537, 'B': 539, 'C': 542, 'E': 536,
                'G': 543, 'H': 543, 'J': 543,
                'K': 541, 'L': 541, 'M': 541, 'N': 541, 'P': 541,
                'R': 535, 'S': 544, 'T': 533, 'V': 534,
            }
            sid = _map.get(pc[0])
            if sid:
                state = State.browse(sid).exists()
                if state:
                    return state, state.country_id

        ca = Country.search([('code', '=', 'CA')], limit=1)
        return None, ca

    def _get_vendor_profile(self, vendor_name):
        """Return the vendor profile dict from KB, or None if not found."""
        if not (vendor_name or '').strip():
            return None
        entry = self.env['premafirm.ml.knowledge'].search([
            ('knowledge_type', '=', 'bill_import'),
            ('input_context', '=', f'VENDOR_PROFILE: {vendor_name.strip()}'),
        ], limit=1)
        if not entry:
            entry = self.env['premafirm.ml.knowledge'].search([
                ('knowledge_type', '=', 'bill_import'),
                ('input_context', 'ilike', f'VENDOR_PROFILE: {vendor_name.strip()[:20]}'),
            ], limit=1)
        if not entry:
            return None
        try:
            import json as _j
            return _j.loads(entry.good_output)
        except Exception:
            return None

    def _build_bill_lines(self, data, attachment):
        """
        Build invoice_line_ids from AI-extracted JSON.

        For fuel lines: uses the correct product (Diesel Fuel / DEF Fuel) so the
        bill matches the pattern from existing posted invoices. Account and tax
        are always set explicitly — product is used for categorisation only.
        For other lines: uses the vendor profile default product/account if known.
        """
        company = self.env.company

        # Vendor profile — applies to non-fuel lines when no explicit product extracted
        vendor_profile = self._get_vendor_profile(data.get('vendor_name', ''))
        profile_product = None
        profile_account = None
        profile_tax     = None
        if vendor_profile:
            pname = vendor_profile.get('default_product')
            if pname:
                profile_product = self.env['product.product'].sudo().search([
                    ('name', '=', pname),
                ], limit=1) or None
            pacc = vendor_profile.get('default_account')
            if pacc:
                profile_account = self.env['account.account'].search([
                    ('code', '=like', f'{pacc}%'),
                    ('deprecated', '=', False),
                    ('company_ids', 'in', company.id),
                ], limit=1) or None
            ptax = vendor_profile.get('default_tax')
            if ptax:
                profile_tax = self.env['account.tax'].search([
                    ('name', '=', ptax),
                    ('type_tax_use', '=', 'purchase'),
                    ('company_id', '=', company.id),
                ], limit=1) or None

        # Detect if this bill is a fuel purchase
        fuel_type_raw = (data.get('fuel_type') or '').upper()
        is_def   = 'DEF' in fuel_type_raw
        is_diesel = not is_def and bool(fuel_type_raw) or any(
            k in (item.get('description') or '').upper()
            for item in (data.get('line_items') or [])
            for k in self._FUEL_KEYWORDS
        )

        diesel_product = self.env['product.product'].browse(self._DIESEL_PRODUCT_ID).exists() or None
        def_product    = self.env['product.product'].browse(self._DEF_PRODUCT_ID).exists() or None

        # Default expense account fallback
        default_account = self.env['account.account'].search([
            ('account_type', '=', 'expense'),
            ('deprecated', '=', False),
            ('company_ids', 'in', company.id),
        ], limit=1)

        lines = []
        for item in (data.get('line_items') or [])[:25]:
            raw_desc  = (item.get('description') or '').strip()
            qty        = float(item.get('quantity') or 1) or 1
            unit_price = (
                float(item.get('unit_price') or 0)
                or (float(item.get('amount') or 0) / qty)
            )

            # Determine if this specific line is fuel
            desc_up    = raw_desc.upper()
            line_is_fuel = any(k in desc_up for k in self._FUEL_KEYWORDS)

            # Product selection
            product = None
            if line_is_fuel or (is_def and not line_is_fuel and not raw_desc):
                product = def_product if ('DEF' in desc_up or is_def) else diesel_product
            elif profile_product and not product:
                # Non-fuel line — use vendor's default product if AI didn't identify one
                product = profile_product

            # Description: product name for fuel, raw description otherwise
            if product and line_is_fuel:
                desc = product.name
            else:
                desc = raw_desc or (attachment.name or 'Service')

            # Account — AI suggestion → vendor profile default → global default
            acc_code = (item.get('suggested_account_code') or '').strip()
            account  = None
            if acc_code:
                account = self.env['account.account'].search([
                    ('code', '=like', f'{acc_code}%'),
                    ('deprecated', '=', False),
                    ('company_ids', 'in', company.id),
                ], limit=1)
            if not account and not line_is_fuel and profile_account:
                account = profile_account
            if not account:
                account = default_account
            if not account:
                continue

            line_vals = {
                'name':       desc,
                'quantity':   qty,
                'price_unit': unit_price,
                'account_id': account.id,
            }
            if product:
                line_vals['product_id'] = product.id

            # Tax — AI suggestion → vendor profile default
            tax_code = (item.get('tax_code') or '').strip()
            tax = None
            if tax_code:
                tax = self.env['account.tax'].search([
                    ('name', '=', tax_code),
                    ('type_tax_use', '=', 'purchase'),
                    ('company_id', '=', company.id),
                ], limit=1)
                if not tax:
                    tax = self.env['account.tax'].search([
                        ('name', 'ilike', tax_code),
                        ('type_tax_use', '=', 'purchase'),
                        ('company_id', '=', company.id),
                    ], limit=1)
            if not tax and not line_is_fuel and profile_tax:
                tax = profile_tax
            if tax:
                line_vals['tax_ids'] = [(4, tax.id)]

            lines.append((0, 0, line_vals))

        # Fallback: single line from the total if no items were parsed
        if not lines and default_account:
            total   = float(data.get('total_amount') or data.get('subtotal') or 0)
            product = diesel_product if (is_diesel and not is_def) else (def_product if is_def else None)
            fb_vals = {
                'name':       product.name if product else (attachment.name or 'Service'),
                'quantity':   1,
                'price_unit': total,
                'account_id': default_account.id,
            }
            if product:
                fb_vals['product_id'] = product.id
            lines = [(0, 0, fb_vals)]

        return lines

    # ── Cron ───────────────────────────────────────────────────────

    @api.model
    def action_cron_process_bills_entry(self):
        """
        Scheduled: pick up unprocessed binary documents from bills-entry folder
        and run AI extraction. Processes up to 20 documents per run.
        """
        bills_entry = self.env.ref(
            'premafirm_ai_engine.documents_folder_bills_entry', raise_if_not_found=False)
        if not bills_entry:
            _logger.info('Bill scan cron: bills-entry folder not found — skipping.')
            return

        docs = self.search([
            ('folder_id', '=', bills_entry.id),
            ('x_processed', '=', False),
            ('type', '=', 'binary'),
        ], limit=20)

        if not docs:
            return

        _logger.info('Bill scan cron: processing %d document(s) from bills-entry', len(docs))
        docs.action_process_with_ml()
