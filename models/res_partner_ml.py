import re
import logging
from odoo import models

_logger = logging.getLogger(__name__)

# Company-name keyword → tag name mapping for zero-cost rule-based tagging.
# Tags are matched case-insensitively against the partner's company/own name.
_NAME_TAG_RULES = [
    (re.compile(r'\b(trucking|transport|transportation|carrier|hauling|haulage)\b', re.I), 'Carrier'),
    (re.compile(r'\bbroker\b', re.I), 'Broker'),
    (re.compile(r'\b(logistics|3pl|third.?party)\b', re.I), 'Logistics'),
    (re.compile(r'\b(freight.?forward|forwarder|forwarding)\b', re.I), 'Freight Forwarder'),
    (re.compile(r'\b(warehouse|warehousing|storage|distribution)\b', re.I), 'Warehouse'),
    (re.compile(r'\b(manufactur|factory|plant|industrial)\b', re.I), 'Manufacturer'),
    (re.compile(r'\b(retail|store|shop|supermarket|grocery)\b', re.I), 'Retail'),
    (re.compile(r'\b(wholesale|distributor)\b', re.I), 'Wholesale'),
]


class ResPartnerML(models.Model):
    _inherit = 'res.partner'

    def action_ml_suggest_tags(self):
        """Manually generate AI tag suggestions for this contact (staff-triggered, uses GPT)."""
        self.ensure_one()
        draft, err = self.env['premafirm.ml.engine'].suggest_customer_tags(self)
        if err or not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'ML Tag Suggestion Failed',
                           'message': err or 'Could not generate tag suggestions.',
                           'type': 'warning', 'sticky': False},
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_ml_batch_tag_partners(self):
        """Cron entry point: apply rule-based tags to untagged partners with real interactions.

        Zero API cost — uses deterministic rules based on company name, invoice history,
        CRM history, and WA negotiation history. Processes up to 50 partners per run.
        Manual AI tag suggestions are still available via the 'Suggest Tags' button.
        """
        # Build a name→id map for tags that exist in the system
        all_tags = self.env['res.partner.category'].sudo().search([])
        tag_map = {t.name.strip().lower(): t.id for t in all_tags}

        def _resolve_tag_ids(tag_names):
            """Return (4, id) commands for tag names that exist in this Odoo instance."""
            return [(4, tag_map[n.lower()]) for n in tag_names if n.lower() in tag_map]

        # Find untagged partners with real business history
        self.env.cr.execute("""
            SELECT DISTINCT rp.id
            FROM res_partner rp
            WHERE rp.active = true
              AND rp.id NOT IN (
                  SELECT partner_id FROM res_partner_res_partner_category_rel
              )
              AND (
                  EXISTS (SELECT 1 FROM sale_order so WHERE so.partner_id = rp.id LIMIT 1)
                  OR EXISTS (SELECT 1 FROM account_move am WHERE am.partner_id = rp.id
                             AND am.move_type IN ('out_invoice','in_invoice') LIMIT 1)
                  OR EXISTS (SELECT 1 FROM crm_lead cl WHERE cl.partner_id = rp.id LIMIT 1)
              )
            LIMIT 50
        """)
        partner_ids = [r[0] for r in self.env.cr.fetchall()]
        partners = self.browse(partner_ids)
        tagged = 0

        for partner in partners:
            try:
                tags_to_add = []

                # ── Determine display name to match against ──────────────
                check_name = (partner.name or '')
                if partner.parent_id:
                    check_name = f"{partner.parent_id.name or ''} {check_name}"

                # ── Rule 1: Company name keyword rules ───────────────────
                for pattern, tag_name in _NAME_TAG_RULES:
                    if pattern.search(check_name):
                        tags_to_add.append(tag_name)

                # ── Rule 2: Has customer invoices → Customer ─────────────
                has_invoice = self.env['account.move'].sudo().search_count([
                    ('partner_id', '=', partner.id),
                    ('move_type', 'in', ['out_invoice', 'out_refund']),
                    ('state', '=', 'posted'),
                ], limit=1)
                if has_invoice:
                    tags_to_add.append('Customer')

                # ── Rule 3: Has vendor bills → Vendor ────────────────────
                has_bill = self.env['account.move'].sudo().search_count([
                    ('partner_id', '=', partner.id),
                    ('move_type', 'in', ['in_invoice', 'in_refund']),
                    ('state', '=', 'posted'),
                ], limit=1)
                if has_bill:
                    tags_to_add.append('Vendor')

                # ── Rule 4: Has CRM leads → Prospect ─────────────────────
                has_lead = self.env['crm.lead'].sudo().search_count([
                    ('partner_id', '=', partner.id),
                ], limit=1)
                if has_lead:
                    tags_to_add.append('Prospect')

                if not tags_to_add:
                    continue

                cmds = _resolve_tag_ids(list(dict.fromkeys(tags_to_add)))  # deduplicate
                if cmds:
                    partner.sudo().write({'category_id': cmds})
                    tagged += 1

            except Exception:
                _logger.exception('Rule-based tagging failed for partner %s', partner.id)

        _logger.info('ML batch tag: applied rule-based tags to %d partners (0 API calls)', tagged)
