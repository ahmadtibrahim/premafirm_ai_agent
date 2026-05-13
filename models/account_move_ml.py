import json
import logging
from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


class AccountMoveML(models.Model):
    _inherit = 'account.move'

    ifta_log_count = fields.Integer(
        compute='_compute_ifta_log_count', string='IFTA Entries')

    def _compute_ifta_log_count(self):
        IFTA = self.env['premafirm.ifta.fuel.log']
        for rec in self:
            rec.ifta_log_count = IFTA.search_count([
                ('account_move_id', '=', rec.id)]) if rec.id else 0

    def action_view_ifta_log(self):
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'name':      'IFTA Fuel Log',
            'res_model': 'premafirm.ifta.fuel.log',
            'view_mode': 'list,form',
            'domain':    [('account_move_id', '=', self.id)],
            'context':   {'default_account_move_id': self.id},
        }

    def action_post(self):
        result = super().action_post()

        # Auto-flag customer invoices/bills if enabled
        auto_flag = self.env['ir.config_parameter'].sudo().get_param(
            'premafirm_ml.auto_flag_invoices', 'False')
        if auto_flag.strip().lower() in ('true', '1', 'yes'):
            for move in self.filtered(lambda m: m.move_type in (
                    'out_invoice', 'in_invoice', 'out_refund', 'in_refund')):
                try:
                    self.env['premafirm.ml.engine'].flag_invoice(move)
                except Exception:
                    pass

        # Auto-ingest posted vendor bills into the ML knowledge base (zero API cost).
        for move in self.filtered(lambda m: m.move_type == 'in_invoice' and m.partner_id):
            try:
                move._save_bill_to_ml()
            except Exception:
                _logger.exception('Bill ML auto-ingest failed for %s', move.name)

        # Track differences vs AI-generated values when confirming AI-sourced bills.
        for move in self.filtered(
            lambda m: m.move_type == 'in_invoice' and m.premafirm_ml_knowledge_id
        ):
            try:
                move._track_ai_vs_actual_diff()
            except Exception:
                _logger.exception('AI diff tracking failed for %s', move.name)

        return result

    def write(self, vals):
        # ── Detect vendor bill partner corrections ────────────────────────────
        partner_before = {}
        if 'partner_id' in vals:
            new_pid = vals.get('partner_id')
            for rec in self.filtered(
                lambda m: m.move_type == 'in_invoice' and m.id and m.partner_id.id != new_pid
            ):
                partner_before[rec.id] = (rec.partner_id.id, rec.partner_id.name)

        # ── Snapshot invoice lines before write (account + product) ──────────
        vendor_bills_before = {}
        if 'invoice_line_ids' in vals:
            for rec in self.filtered(
                lambda m: m.move_type == 'in_invoice' and m.partner_id and m.id
            ):
                vendor_bills_before[rec.id] = {
                    ln.id: (ln.account_id.code, ln.product_id.id if ln.product_id else None)
                    for ln in rec.invoice_line_ids
                    if ln.account_id and ln.display_type == 'product'
                }

        result = super().write(vals)

        # ── Save partner corrections to ML ────────────────────────────────────
        if partner_before:
            for rec in self.filtered(lambda m: m.id in partner_before):
                old_pid, old_name = partner_before[rec.id]
                try:
                    rec._save_bill_to_ml(
                        correction_note=f"Staff corrected vendor: [{old_name}] → [{rec.partner_id.name}]"
                    )
                except Exception:
                    _logger.exception('Bill ML vendor correction save failed for %s', rec.name)

        # ── Save account / product corrections to ML ──────────────────────────
        if vendor_bills_before:
            for rec in self.filtered(
                lambda m: m.id in vendor_bills_before and m.move_type == 'in_invoice'
            ):
                before = vendor_bills_before[rec.id]
                after  = {
                    ln.id: (ln.account_id.code, ln.product_id.id if ln.product_id else None)
                    for ln in rec.invoice_line_ids
                    if ln.account_id and ln.display_type == 'product'
                }
                before_sig = set(before.values())
                after_sig  = set(after.values())
                if before_sig != after_sig:
                    old_acc = ', '.join(sorted(str(v[0]) for v in before_sig))
                    new_acc = ', '.join(sorted(str(v[0]) for v in after_sig))
                    note = f"Staff corrected lines: accounts [{old_acc}] → [{new_acc}]"
                    try:
                        rec._save_bill_to_ml(correction_note=note)
                    except Exception:
                        _logger.exception('Bill ML correction save failed for %s', rec.name)

        return result

    def _save_bill_to_ml(self, correction_note=None):
        """
        Upsert this vendor bill's account pattern into the ML knowledge base.

        Called automatically on action_post and whenever account lines change.
        No API calls — pure structured DB data.  The bill scan service uses
        this knowledge to pre-fill accounts on future scans for the same vendor.
        """
        if self.move_type != 'in_invoice' or not self.partner_id:
            return

        lines_data = [
            {
                'description':   (ln.name or '')[:60],
                'account_code':  ln.account_id.code,
                'account_name':  ln.account_id.name,
                'product_id':    ln.product_id.id if ln.product_id else None,
                'product_name':  ln.product_id.name if ln.product_id else None,
                'tax_names':     [t.name for t in ln.tax_ids],
            }
            for ln in self.invoice_line_ids
            if ln.account_id and ln.display_type == 'product'
        ]
        if not lines_data:
            return

        account_codes = [d['account_code'] for d in lines_data]
        product_names = [d['product_name'] for d in lines_data if d['product_name']]
        # Include parent vendor name for multi-location vendors
        vendor_name = (
            self.partner_id.parent_id.name if self.partner_id.parent_id
            else self.partner_id.name
        )
        input_ctx = (
            f"Vendor: {vendor_name} | "
            f"Invoice: {self.ref or self.name or ''} | "
            f"Total: {self.amount_total} {self.currency_id.name if self.currency_id else 'CAD'} | "
            f"Accounts: {', '.join(account_codes)}"
            + (f" | Products: {', '.join(product_names)}" if product_names else '')
        )
        good_output = json.dumps({
            'vendor_name':    vendor_name,
            'partner_id':     self.partner_id.id,
            'partner_name':   self.partner_id.name,
            'invoice_number': self.ref or self.name or '',
            'invoice_date':   str(self.invoice_date or ''),
            'total':          self.amount_total,
            'currency':       self.currency_id.name if self.currency_id else 'CAD',
            'account_codes':  account_codes,
            'lines':          lines_data,
        }, indent=2)

        KB = self.env['premafirm.ml.knowledge'].sudo()
        ref_key = (self.ref or self.name or '').strip()
        existing = KB.browse()
        if ref_key:
            existing = KB.search([
                ('knowledge_type', '=', 'bill_import'),
                ('input_context', 'like', f"Vendor: {vendor_name} |%"),
                ('input_context', 'like', f"% Invoice: {ref_key} |%"),
            ], limit=1)

        if existing:
            existing.write({
                'good_output':    good_output,
                'origin':         'bill_edit' if correction_note else existing.origin,
                'correction_note': correction_note or existing.correction_note,
                'weight':         min(existing.weight + 0.3, 5.0),
            })
        else:
            KB.create({
                'knowledge_type': 'bill_import',
                'input_context':  input_ctx,
                'good_output':    good_output,
                'origin':         'bill_edit' if correction_note else 'approved',
                'correction_note': correction_note,
                'weight':         1.5,
            })

    def action_autofill_from_attachment(self):
        """
        Find the first binary attachment on this vendor bill, extract its text
        using free OCR/PDF parsing, then ask GPT (text model) to structure the
        data. Opens the resulting draft for human review before applying.
        """
        self.ensure_one()
        if self.move_type != 'in_invoice':
            raise exceptions.UserError(
                'Bill auto-fill is only available on vendor bills.')

        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', self.id),
            ('type', '=', 'binary'),
        ], order='id desc', limit=1)

        if not attachment or not attachment.datas:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No Attachment Found',
                    'message': 'Please upload a PDF or image first, then click Fill from Attachment.',
                    'type': 'warning',
                    'sticky': False,
                },
            }

        draft, err = self.env['premafirm.ml.engine'].generate_bill_autofill(
            attachment.datas,
            attachment.mimetype or '',
            attachment.name or '',
            self,
        )

        if err or not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Extraction Failed',
                    'message': err or 'Could not extract data from attachment.',
                    'type': 'warning',
                    'sticky': True,
                },
            }

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_ml_flag_invoice(self):
        """Manually trigger ML invoice review."""
        self.ensure_one()
        draft, err = self.env['premafirm.ml.engine'].flag_invoice(self)
        if err:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'ML Error', 'message': err,
                           'type': 'warning', 'sticky': False},
            }
        if not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'No Issues Found',
                           'message': 'This invoice looks normal — no anomalies detected.',
                           'type': 'success', 'sticky': False},
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _track_ai_vs_actual_diff(self):
        """Compare confirmed bill values against original AI-generated values.

        Updates the linked ML knowledge entry with diffs so future predictions improve.
        """
        knowledge = self.premafirm_ml_knowledge_id
        if not knowledge or not knowledge.good_output:
            return

        try:
            ai_vals = json.loads(knowledge.good_output)
        except Exception:
            return

        diffs = []

        # Vendor diff
        ai_vendor = str(ai_vals.get("vendor_name") or ai_vals.get("partner_name") or "")
        actual_vendor = self.partner_id.name if self.partner_id else ""
        if ai_vendor and actual_vendor and ai_vendor.lower() != actual_vendor.lower():
            diffs.append(f"vendor: AI='{ai_vendor}' → actual='{actual_vendor}'")

        # Amount diff (>2% threshold to ignore rounding)
        ai_amount = float(ai_vals.get("total") or 0)
        actual_amount = float(self.amount_total or 0)
        if ai_amount and abs(ai_amount - actual_amount) / max(ai_amount, 0.01) > 0.02:
            diffs.append(f"amount: AI={ai_amount:.2f} → actual={actual_amount:.2f}")

        # Date diff
        ai_date = str(ai_vals.get("invoice_date") or "")
        actual_date = str(self.invoice_date or "")
        if ai_date and actual_date and ai_date != actual_date:
            diffs.append(f"date: AI='{ai_date}' → actual='{actual_date}'")

        # Account codes diff
        ai_accounts = set(ai_vals.get("account_codes") or [])
        actual_accounts = {
            ln.account_id.code
            for ln in self.invoice_line_ids
            if ln.account_id and ln.display_type == 'product'
        }
        added = actual_accounts - ai_accounts
        removed = ai_accounts - actual_accounts
        if added or removed:
            diffs.append(
                f"accounts: removed={sorted(removed) or '[]'} added={sorted(added) or '[]'}"
            )

        if diffs:
            diff_note = "On confirmation: " + "; ".join(diffs)
            existing_note = knowledge.correction_note or ""
            combined = f"{existing_note}\n{diff_note}".strip()
            knowledge.write({
                "correction_note": combined,
                "origin":          "edited",
                "weight":          min(knowledge.weight + 0.5, 5.0),
            })
        else:
            knowledge.write({
                "origin": "approved",
                "weight": min(knowledge.weight + 0.1, 5.0),
            })
