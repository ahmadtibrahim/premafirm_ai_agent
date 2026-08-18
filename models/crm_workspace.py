"""PHASES 16-17 — CRM daily workspace + response analytics.

PHASE 16: priority-bucket field + search facets + saved filters give the
daily workspace; the graph view grouped by ``x_ws_bucket`` IS the native
priority dashboard (nine KPI bars — Needs Reply, Follow-Up Today, Overdue,
New Leads, Quotes Waiting, Onboarding, Unassigned, Bounces, Replies Today).

PHASE 17: response analytics — outbound counter + first-outbound stamp
(hook on every outbound path), response-time hours, reply/bounce/OOO
flags, won amount, segment and customer-type breakdowns. Consumed by the
pivot/graph views under CRM → Response Analytics.

All date-dynamic filter domains (Follow-Up Today, Overdue, Stale 7/14,
Won This Month) are evaluated server-side via ``context_today()`` in
ir.filters (see data/crm_workspace_data.xml) or via the searchable
computed fields below — no hardcoded dates anywhere.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# PHASE 13 restructure wrote these tags as the surviving segment context
# (see crm_pipeline._OLD_TO_NEW) — the analytics "segment" dimension.
_SEGMENT_TAGS = (
    'Call Back', 'Call Approach', 'Dedicated Corridors Campaign',
    'Retail', 'Suggestion', 'Callback Request',
)

# Industry-ish tags used as the provisional "customer type" dimension
# (PHASE 18 tag cleanup will move these to structured fields; until then
# the tag taxonomy is the only structured-ish source).
_INDUSTRY_TAGS = (
    'food', 'produce', 'retail', 'broker', 'shipper', '3pl',
    'warehousing', 'dispatcher', 'manufacturer', 'distribution',
    'reefer', 'pharmaceutical', 'logistics', 'carrier', 'b2b',
    'cold storage', 'food & produce', 'direct shipper',
    'food freight', 'storage', 'refrigerated', 'warehouse',
    # PHASE 18 merge created the canonical title-case tags
    'food processing',
)

_BUCKETS = [
    ('replied_today', 'Replies Today'),
    ('needs_reply', 'Needs Reply'),
    ('followup_today', 'Follow-Up Today'),
    ('overdue', 'Overdue'),
    ('bounced', 'Bounces'),
    ('unassigned', 'Unassigned'),
    ('new', 'New Leads'),
    ('quotes', 'Quotes Waiting'),
    ('onboarding', 'Onboarding'),
    ('other', 'Other'),
]
_BUCKET_ORDER = [b[0] for b in _BUCKETS]


class CrmWorkspace(models.Model):
    _inherit = 'crm.lead'

    # ── PHASE 17: outbound tracking (stored, hook-maintained) ──────────
    first_outbound_at = fields.Datetime(
        'First Outbound Message', index=True,
        help='First email we initiated on this lead (any outbound path).')
    x_ana_outbound_count = fields.Integer(
        'Outbound Emails Sent', default=0, index=True,
        help='Total outbound emails on this lead (any path). Backfilled '
             'for existing leads by the 18.0.6.16.0 migration.')

    # ── PHASE 17: response analytics (computed, non-stored) ────────────
    x_ana_first_response_hours = fields.Float(
        'First Response Time (hours)', compute='_compute_ana',
        help='Hours from lead creation (date_open) to our first outbound '
             'email. Blank when we never emailed.')
    x_ana_sales_reply_hours = fields.Float(
        'Sales Reply Time (hours)', compute='_compute_ana',
        help='Hours from the customer\'s last meaningful reply to our '
             'next outbound email. Blank when we have not answered yet.')
    x_ana_replied = fields.Boolean(
        'Meaningful Reply', compute='_compute_ana',
        help='Customer sent a meaningful reply (bounces/OOO never count).')
    x_ana_bounced = fields.Boolean(
        'Bounced', compute='_compute_ana')
    x_ana_ooo = fields.Boolean(
        'OOO / Auto Reply', compute='_compute_ana')
    x_ana_won_amount = fields.Float(
        'Won Revenue', compute='_compute_ana',
        help='expected_revenue when the lead is won, else 0.')

    # ── PHASE 17: breakdown dimensions (computed, non-stored) ──────────
    x_segment = fields.Char(
        'Segment', compute='_compute_dimensions',
        help='PHASE 13 segment tags (Call Back, Call Approach, Dedicated '
             'Corridors Campaign, Retail, Suggestion…).')
    x_customer_type = fields.Char(
        'Customer Type', compute='_compute_dimensions',
        help='Industry-ish tag on the lead/partner. Provisional until '
             'PHASE 18 tag cleanup lands structured fields.')

    # ── PHASE 16: priority bucket (computed, non-stored) ───────────────
    x_ws_bucket = fields.Selection(
        _BUCKETS, 'Priority Bucket', compute='_compute_ws_bucket',
        help='One indicative priority bucket per lead — the native graph '
             'grouped by this field is the priority dashboard. A lead may '
             'fit several; the first match in priority order wins.')

    # ── PHASE 16: searchable date helpers (computed + search methods) ──
    x_ws_outreach_days = fields.Integer(
        'Days Since Last Outreach', compute=lambda self: None,
        search='_search_outreach_days',
        help='Search-only: days since last_outreach_at (Stale 7/14 filters).')
    x_ws_won_this_month = fields.Boolean(
        'Won This Month', compute=lambda self: None,
        search='_search_won_this_month',
        help='Search-only: date_closed falls in the current calendar month.')

    # ── hooks ──────────────────────────────────────────────────────────

    def _message_post_after_hook(self, message, msg_values):
        """Outbound email on ANY path → bump the counter + first stamp
        (the PHASE 8 hook already writes last_outbound/last_outreach and
        runs the response discipline; this adds the analytics fields).
        Outbound = internal-user author, type email/email_outgoing (the
        same discriminator as crm_reply_status)."""
        res = super()._message_post_after_hook(message, msg_values)
        author = message.author_id
        internal = author and any(
            not u.share for u in author.user_ids)
        if (message.message_type in ('email', 'email_outgoing')
                and internal):
            now = fields.Datetime.now()
            for lead in self:
                vals = {'x_ana_outbound_count':
                        (lead.x_ana_outbound_count or 0) + 1}
                if not lead.first_outbound_at:
                    vals['first_outbound_at'] = now
                lead.write(vals)
        return res

    # ── computes ───────────────────────────────────────────────────────

    @api.depends('first_outbound_at', 'date_open',
                 'last_meaningful_reply_at', 'last_outbound_at',
                 'last_inbound_classification', 'stage_id.is_won',
                 'expected_revenue')
    def _compute_ana(self):
        for lead in self:
            lead.x_ana_first_response_hours = (
                (lead.first_outbound_at - lead.date_open).total_seconds()
                / 3600.0
                if lead.first_outbound_at and lead.date_open
                and lead.first_outbound_at > lead.date_open else 0.0)
            last_in, last_out = lead.last_meaningful_reply_at, lead.last_outbound_at
            lead.x_ana_sales_reply_hours = (
                (last_out - last_in).total_seconds() / 3600.0
                if last_in and last_out and last_out > last_in else 0.0)
            lead.x_ana_replied = bool(last_in)
            lead.x_ana_bounced = (
                lead.last_inbound_classification in ('bounce',
                                                     'delivery_failure'))
            lead.x_ana_ooo = (
                lead.last_inbound_classification in ('out_of_office',
                                                     'auto_reply'))
            lead.x_ana_won_amount = (
                lead.expected_revenue if lead.stage_id.is_won else 0.0)

    @api.depends('tag_ids.name', 'partner_id')
    def _compute_dimensions(self):
        for lead in self:
            names = [t.name for t in lead.tag_ids if t.name]
            low = [n.strip().lower() for n in names]
            seg = [n for n, l in zip(names, low)
                   if n.strip() in _SEGMENT_TAGS
                   or n.strip().lower() in _SEGMENT_TAGS]
            lead.x_segment = ', '.join(seg) or False
            industry = next((n for n in names
                             if n.strip().lower() in _INDUSTRY_TAGS),
                            False)
            lead.x_customer_type = industry or 'Other'

    @api.depends('needs_reply', 'user_id', 'next_followup_at',
                 'last_inbound_classification', 'stage_id.name',
                 'last_meaningful_reply_at', 'active')
    def _compute_ws_bucket(self):
        now = fields.Datetime.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        for lead in self:
            bucket = 'other'
            if lead.last_meaningful_reply_at and day_start <= lead.last_meaningful_reply_at < day_end:
                bucket = 'replied_today'
            elif lead.needs_reply:
                bucket = 'needs_reply'
            elif lead.next_followup_at and day_start <= lead.next_followup_at < day_end:
                bucket = 'followup_today'
            elif lead.next_followup_at and lead.next_followup_at < day_start:
                bucket = 'overdue'
            elif lead.last_inbound_classification in ('bounce', 'delivery_failure'):
                bucket = 'bounced'
            elif not lead.user_id:
                bucket = 'unassigned'
            elif lead.stage_id.name == 'NEW / UNCONTACTED':
                bucket = 'new'
            elif lead.stage_id.name in ('QUOTE REQUESTED', 'QUOTE SENT',
                                        'NEGOTIATION'):
                bucket = 'quotes'
            elif lead.stage_id.name == 'ONBOARDING':
                bucket = 'onboarding'
            lead.x_ws_bucket = bucket

    # ── search methods (dynamic dates, server-side) ────────────────────

    @api.model
    def _search_outreach_days(self, operator, value):
        # "days since outreach" is the INVERSE of the timestamp operator:
        # ≥ N days  ⇔  last_outreach_at ≤ now − N days (the older the
        # outreach, the higher the day count).
        if operator in ('=', '!='):
            op = '=' if operator == '=' else '!='
            cutoff = fields.Datetime.now() - timedelta(days=int(value or 0))
            return [('last_outreach_at', op, cutoff)]
        op = {'>': '<', '>=': '<=', '<': '>', '<=': '>='}[operator]
        cutoff = fields.Datetime.now() - timedelta(days=int(value or 0))
        return [('last_outreach_at', op, cutoff)]

    @api.model
    def _search_won_this_month(self, operator, value):
        now = fields.Datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0,
                                  microsecond=0)
        if operator == '=' and value:
            return [('date_closed', '>=', month_start)]
        if operator == '!=' and value:
            return ['|', ('date_closed', '<', month_start),
                    ('date_closed', '=', False)]
        if operator in ('<', '<=') and not value:
            return [('date_closed', '<', month_start)]
        return [('id', '=', False)]
