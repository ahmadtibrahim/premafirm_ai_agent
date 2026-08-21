import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    # ── Business reference fields ──────────────────────────────
    company_currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
        store=True,
    )
    final_rate = fields.Monetary(currency_field="company_currency_id", default=0.0)
    product_id = fields.Many2one("product.product", string="Freight Product")
    po_number = fields.Char("Customer PO #")
    bol_number = fields.Char("BOL #")
    pod_reference = fields.Char("POD Reference")
    payment_terms = fields.Many2one("account.payment.term", string="Payment Terms")

    # Backward-compatible aliases
    premafirm_po = fields.Char(related="po_number", store=True, readonly=False)
    premafirm_bol = fields.Char(related="bol_number", store=True, readonly=False)
    premafirm_pod = fields.Char(related="pod_reference", store=True, readonly=False)

    # reply_received is now computed/stored from last_meaningful_reply_at
    # (see crm_reply_status.py, PHASE 8).
    next_followup_date = fields.Date("Next Follow-up")
    ai_lead_score = fields.Float("AI Lead Score", digits=(5, 1), default=0.0)

    def action_fetch_emails(self):
        self.env['fetchmail.server']._fetch_mails()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Emails Refreshed",
                "message": "Incoming mail servers have been checked for new emails.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def message_post(self, **kwargs):
        # No email_from injection — Odoo's own default sender is used
        # (business decision 2026-08-20: tag-based From override removed).
        # PHASE 29 — preserve the caller's threading intent for the
        # reply-status hook.  message_post re-computes parent_id under
        # flat threading BEFORE _message_post_after_hook runs, so a fresh
        # composer email and a reply both come back with the same computed
        # parent (the thread's first message) and the hook cannot tell
        # outreach from an answer.  The ORIGINAL values (parent_id /
        # references / in_reply_to as passed by the composer or caller)
        # say whether we continue an existing email thread.
        if ('parent_id' in kwargs or 'references' in kwargs
                or 'in_reply_to' in kwargs):
            self = self.with_context(premafirm_post_intent=bool(
                kwargs.get('parent_id') or kwargs.get('references')
                or kwargs.get('in_reply_to')))
        return super().message_post(**kwargs)

    # ── Tag inheritance from the linked contact/company ───────────
    # Mirrors partner_id.category_id (Contact Tags, incl. the Province/City
    # tags stamped on the parent company) onto this lead's CRM Tags.

    def _sync_tags_from_partner(self):
        Tag = self.env['crm.tag']
        tag_cache = {}
        for lead in self:
            partner = lead.partner_id
            if not partner or not partner.category_id:
                continue
            tag_ids = []
            for cat in partner.category_id:
                name = (cat.name or '').strip()
                if not name:
                    continue
                if name not in tag_cache:
                    tag = Tag.search([('name', '=', name)], limit=1)
                    if not tag:
                        tag = Tag.create({'name': name})
                    tag_cache[name] = tag.id
                tag_ids.append(tag_cache[name])
            missing = set(tag_ids) - set(lead.tag_ids.ids)
            if missing:
                lead.write({'tag_ids': [(4, tid) for tid in missing]})

    @api.model_create_multi
    def create(self, vals_list):
        # PHASE 11 — the routed new-inquiry path passes the sender's
        # partner id in this marker key (popped before super so the
        # unknown field never reaches the DB).
        attach = [vals.pop('premafirm_attach_contact', False)
                  for vals in vals_list]
        leads = super().create(vals_list)
        for lead, author in zip(leads, attach):
            # company → contacts: the opportunity's partner is the COMPANY;
            # a contact-child sender is tracked as a contact row instead.
            if lead.partner_id and lead.partner_id.parent_id:
                lead.write({'partner_id': lead.partner_id.parent_id.id})
            if author:
                self.env['crm.lead.contact']._attach_sender(lead.id, author)
        leads.filtered('partner_id')._sync_tags_from_partner()
        return leads

    def write(self, vals):
        result = super().write(vals)
        if 'partner_id' in vals:
            self.filtered('partner_id')._sync_tags_from_partner()
        return result
