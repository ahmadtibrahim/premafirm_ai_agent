import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_INC_TAGS = frozenset({'b2b', 'retail', 'wholesale'})

_LOGISTICS_TAGS = frozenset({
    'logistics', 'carrier', 'broker', 'freight forwarder',
    'freight broker', 'freight', '3pl', 'third-party logistics',
})


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

    reply_received = fields.Boolean("Reply Received", default=False)
    next_followup_date = fields.Date("Next Follow-up")
    ai_lead_score = fields.Float("AI Lead Score", digits=(5, 1), default=0.0)

    # ── Email segment (tag-based routing) ─────────────────────────
    email_segment = fields.Selection(
        [
            ('inc', 'INC — premafirm.com'),
            ('logistics', 'Logistics — logistics.premafirm.com'),
        ],
        string="Email Segment",
        compute="_compute_email_segment",
        store=False,
        help="Detected from partner tags. Controls which From address is used when emailing this lead.",
    )

    @api.depends("partner_id.category_id.name")
    def _compute_email_segment(self):
        for lead in self:
            tag_names = {t.name.lower() for t in (lead.partner_id.category_id or [])}
            if tag_names & _INC_TAGS:
                lead.email_segment = 'inc'
            elif tag_names & _LOGISTICS_TAGS:
                lead.email_segment = 'logistics'
            else:
                lead.email_segment = False

    def _segment_email_from(self):
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        if self.email_segment == 'inc':
            return ICP.get_param('premafirm.email_from_inc', 'sales@premafirm.com')
        if self.email_segment == 'logistics':
            return ICP.get_param('premafirm.email_from_logistics', 'dispatch@logistics.premafirm.com')
        return None

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
        if len(self) == 1 and not kwargs.get('email_from'):
            from_addr = self._segment_email_from()
            if from_addr:
                kwargs['email_from'] = from_addr
        return super().message_post(**kwargs)
