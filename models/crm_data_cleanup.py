"""PHASE 19 — data cleanup REVIEW reports (review-only, never auto-fix).

The rule: report first, human decides, archive never delete. This module
adds the search vocabulary that turns each issue class into a facet:

  * Duplicate Email   — same email on 2+ leads (NEVER auto-merge on email
                        match alone — the spec forbids it)
  * RE:/FWD: Subject  — a reply/forward that became a lead (threading was
                        lost at import time)
  * queue review states — Duplicate Reply / Bounce Merged join the
                        Inbound Review Queue state machine so a human
                        reviewer can record the disposition of each row
                        (Needs Manual Review = state 'new')

Nothing here deletes or merges data. The review report with live counts
is generated separately (crm_uat_tools/PHASE1819_CLEANUP_REVIEW.md).
"""
from odoo import api, fields, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # Search-only review flags — the facet domain maps onto the search
    # method, so no stored columns and no per-row maintenance.
    x_cleanup_dup_email = fields.Boolean(
        'Duplicate Email (review)', compute=lambda self: None,
        search='_search_cleanup_dup_email',
        help='Review-only: this email exists on 2+ leads. Never auto-merge '
             'on email match alone.')
    x_cleanup_re_fwd = fields.Boolean(
        'RE:/FWD: Subject (review)', compute=lambda self: None,
        search='_search_cleanup_re_fwd',
        help='Review-only: subject starts with RE:/FWD: — a reply that '
             'became a lead (threading lost).')

    @api.model
    def _search_cleanup_dup_email(self, operator, value):
        if operator != '=' or not value:
            return [('id', '=', False)]
        self.env.cr.execute("""
            SELECT id FROM crm_lead
            WHERE coalesce(email_from, '') != ''
              AND lower(email_from) IN (
                  SELECT lower(email_from) FROM crm_lead
                  WHERE coalesce(email_from, '') != ''
                  GROUP BY lower(email_from) HAVING count(*) > 1)""")
        ids = [r[0] for r in self.env.cr.fetchall()]
        return [('id', 'in', ids)] if ids else [('id', '=', False)]

    @api.model
    def _search_cleanup_re_fwd(self, operator, value):
        if operator != '=' or not value:
            return [('id', '=', False)]
        return ['|', '|',
                ('name', '=ilike', 're:%'),
                ('name', '=ilike', 'fwd:%'),
                ('name', '=ilike', 'fw:%')]


class PremafirmInboundQueue(models.Model):
    _inherit = 'premafirm.inbound.queue'

    # PHASE 19 — reviewer disposition states for the cleanup pass.
    # ondelete='set default' required by Odoo 18: selection_add values on
    # a REQUIRED selection must not fall back to 'set null' — the base
    # field's default ('new') is the fallback.
    state = fields.Selection(
        selection_add=[
            ('duplicate_reply', 'Duplicate Reply'),
            ('bounce_merged', 'Bounce Merged'),
        ],
        ondelete={'duplicate_reply': 'set default',
                  'bounce_merged': 'set default'})

    def action_mark_duplicate_reply(self):
        self.write({'state': 'duplicate_reply',
                    'review_note': 'Marked as duplicate reply by reviewer.'})
        return True

    def action_mark_bounce_merged(self):
        self.write({'state': 'bounce_merged',
                    'review_note': 'Marked as bounce (merged with the '
                                   'lead\'s bounce evidence) by reviewer.'})
        return True
