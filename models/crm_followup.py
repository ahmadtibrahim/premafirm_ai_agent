"""
CRM Follow-up Workflow + Cold Lead Reactivation
Daily cron: generates AI follow-up drafts at 3 and 8 business days of silence.
Weekly cron: surfaces cold leads (60+ days) with AI reactivation drafts.
"""
import logging
from datetime import timedelta, date

from markupsafe import Markup
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

SEASONAL = {
    3: "Produce season approaching — food distributors and grocers need reefer capacity.",
    4: "Peak produce season — temperature-controlled freight demand is high.",
    5: "Peak produce season — prioritize food/produce leads for reefer lanes.",
    6: "Late produce season — still strong for reefer.",
    10: "Pre-holiday surge — retailers and distributors building inventory.",
    11: "Holiday freight peak — high demand across all lanes.",
}


def _biz_days_since(dt):
    if not dt:
        return 0
    now = fields.Datetime.now()
    if dt >= now:
        return 0
    current = dt.date()
    end = now.date()
    count = 0
    while current < end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _api_key(env):
    from odoo.addons.premafirm_ai_engine.services.deepseek_utils import get_api_key as _get_deepseek_key
    return _get_deepseek_key(env)


def _gpt_draft(env, system, user_msg, max_tokens=300):
    try:
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat
        key = _api_key(env)
        if not key:
            return None
        return deepseek_chat(
            messages=[{'role': 'user', 'content': user_msg}],
            system=system,
            max_tokens=max_tokens,
            api_key=key,
        )
    except Exception as exc:
        _logger.warning('GPT draft error: %s', exc)
        return None


def _get_thread(lead, limit=6):
    import re
    snippets = []
    for msg in lead.message_ids.sorted('date', reverse=True):
        if msg.message_type not in ('email', 'comment'):
            continue
        body = re.sub(r'<[^>]+>', ' ', msg.body or '')
        body = re.sub(r'\s+', ' ', body).strip()
        if not body or len(body) < 10:
            continue
        direction = 'SENT' if (msg.author_id and msg.author_id.user_ids) else 'RECEIVED'
        ts = msg.date.strftime('%Y-%m-%d') if msg.date else '?'
        snippets.append(f'[{direction} {ts}]: {body[:300]}')
        if len(snippets) >= limit:
            break
    return '\n'.join(snippets) or 'No messages found.'


class CrmFollowupCron(models.Model):
    _inherit = 'crm.lead'

    @api.model
    def run_followup_cron(self):
        """Daily cron: post AI follow-up drafts for leads with no reply."""
        leads = self.sudo().search([
            ('active', '=', True),
            ('x_response_status', '=', 'none'),
            ('x_last_outreach_at', '!=', False),
            ('date_closed', '=', False),
        ])
        leads = leads.filtered(lambda l: l.probability not in (100,))

        for lead in leads:
            try:
                days = _biz_days_since(lead.x_last_outreach_at)
                if days >= 3 and not lead.x_followup_1_sent_at:
                    lead._post_followup(1, days)
                    lead.sudo().write({'x_followup_1_sent_at': fields.Datetime.now()})
                elif (lead.x_followup_1_sent_at
                      and not lead.x_followup_2_sent_at
                      and _biz_days_since(lead.x_followup_1_sent_at) >= 5):
                    lead._post_followup(2, days)
                    lead.sudo().write({'x_followup_2_sent_at': fields.Datetime.now()})
            except Exception as exc:
                _logger.warning('Follow-up cron error lead %s: %s', lead.id, exc)

    def _post_followup(self, num, days):
        contact = self.partner_id
        contact_name = contact.name if contact else self.partner_name or 'the contact'
        company = contact.parent_id if contact and contact.parent_id else None
        company_name = company.name if company else contact_name
        title = contact.function if contact and contact.function else 'unknown title'
        thread = _get_thread(self)

        system = (
            'You are Ahmad Ibrahim\'s AI freight sales assistant at PremaFirm Inc., '
            'a Canadian trucking carrier. Write concise freight sales follow-up emails. '
            'Professional, warm, gets to the point. Under 100 words unless asked otherwise. '
            'Do NOT include a signature — Odoo adds it automatically.'
        )

        if num == 1:
            prompt = (
                f'Write a follow-up email to {contact_name} ({title}) at {company_name}. '
                f'{days} business days since last contact. '
                f'Use a value-add angle — offer something useful (lane capacity, seasonal insight, availability window). '
                f'Do not just say "checking in". Previous thread:\n{thread}'
            )
        else:
            prompt = (
                f'Write a final follow-up email to {contact_name} at {company_name}. '
                f'{days} business days since last contact. Still no reply. '
                f'Mild urgency angle — mention limited capacity or upcoming peak season. '
                f'Clear call to action. Under 80 words. '
                f'Previous thread:\n{thread}'
            )

        draft = _gpt_draft(self.env, system, prompt)

        body = Markup(
            f'<b>📬 AI Follow-up Draft #{num}</b> '
            f'({days} business days since last outreach to {contact_name} at {company_name})<br/>'
            f'<i>Review below and send manually when ready.</i><br/><br/>'
        )
        if draft:
            body += Markup(f'<pre style="white-space:pre-wrap;font-family:inherit">{Markup.escape(draft)}</pre>')
        else:
            body += Markup(
                f'<i>Could not generate draft (API key issue). '
                f'Manually follow up with {contact_name} at {company_name}.</i>'
            )

        self.message_post(body=body, subtype_xmlid='mail.mt_note')

    @api.model
    def run_cold_reactivation_cron(self):
        """Weekly cron: surface cold leads (60+ days) with reactivation drafts."""
        cutoff = fields.Datetime.now() - timedelta(days=60)
        seasonal_hint = SEASONAL.get(date.today().month, '')

        leads = self.sudo().search([
            ('active', '=', True),
            ('x_response_status', '=', 'none'),
            ('x_last_outreach_at', '!=', False),
            ('x_last_outreach_at', '<', cutoff),
            ('date_closed', '=', False),
        ])
        leads = leads.filtered(lambda l: l.probability not in (100,))

        reactivated = 0
        recent_cutoff = fields.Datetime.now() - timedelta(days=30)
        for lead in leads:
            try:
                already = self.env['mail.message'].sudo().search_count([
                    ('res_id', '=', lead.id),
                    ('model', '=', 'crm.lead'),
                    ('body', 'ilike', 'Reactivation'),
                    ('date', '>', recent_cutoff),
                ])
                if already:
                    continue
                lead._post_reactivation(seasonal_hint)
                reactivated += 1
            except Exception as exc:
                _logger.warning('Reactivation error lead %s: %s', lead.id, exc)

        _logger.info('Cold reactivation cron: surfaced %d leads', reactivated)

    def _post_reactivation(self, seasonal_hint=''):
        contact = self.partner_id
        contact_name = contact.name if contact else self.partner_name or 'the contact'
        company = contact.parent_id if contact and contact.parent_id else None
        company_name = company.name if company else contact_name
        days = _biz_days_since(self.x_last_outreach_at)
        thread = _get_thread(self, limit=4)

        seasonal = f'\nSEASONAL CONTEXT: {seasonal_hint}' if seasonal_hint else ''
        prompt = (
            f'Write a reactivation email to {contact_name} at {company_name}. '
            f'{days} business days since last contact — this account has gone cold. '
            f'Reference the previous conversation briefly, offer fresh value. '
            f'Warm but professional. Under 90 words. '
            f'Do not include a signature.{seasonal}\n\n'
            f'Previous thread:\n{thread}'
        )

        draft = _gpt_draft(
            self.env,
            'You are Ahmad Ibrahim\'s AI freight sales assistant. Write reactivation emails that feel personal and relevant, not generic.',
            prompt,
        )

        body = Markup(
            f'<b>🔄 Cold Lead Reactivation</b> — {Markup.escape(company_name)}<br/>'
            f'Last contact: {days} business days ago | Contact: {Markup.escape(contact_name)}<br/>'
        )
        if seasonal_hint:
            body += Markup(f'<i>📅 {Markup.escape(seasonal_hint)}</i><br/>')
        if draft:
            body += Markup(f'<br/><b>AI Draft:</b><br/><pre style="white-space:pre-wrap;font-family:inherit">{Markup.escape(draft)}</pre>')

        self.message_post(body=body, subtype_xmlid='mail.mt_note')

    def _schedule_manual_stage_review(self, summary, note, deadline=None):
        self.ensure_one()
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        if not todo:
            return False
        existing = self.env['mail.activity'].search([
            ('res_model', '=', 'crm.lead'),
            ('res_id', '=', self.id),
            ('summary', '=', summary),
        ], limit=1)
        if existing:
            return False
        self.activity_schedule(
            activity_type_id=todo.id,
            summary=summary,
            note=note,
            date_deadline=deadline or fields.Date.today(),
            user_id=self.user_id.id or self.env.uid,
        )
        return True

    # ── Outreach/Contacted no-reply → Manual review ──────────────────────────

    @api.model
    def run_outreach_stale_cron(self):
        """Daily: stale outreach leads stay in place and get flagged for manual review."""
        stage_ids = self.env['crm.stage'].sudo().search(
            [('name', 'in', ['Outreach', 'Contacted'])]
        ).ids
        if not stage_ids:
            return

        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            threshold_days = profile.icp_non_response_days or 7
        except Exception:
            threshold_days = 7

        cutoff = fields.Datetime.now() - timedelta(days=threshold_days)
        leads = self.sudo().search([
            ('active', '=', True),
            ('stage_id', 'in', stage_ids),
            ('x_response_status', '=', 'none'),
            ('x_last_outreach_at', '!=', False),
            ('x_last_outreach_at', '<=', cutoff),
            ('date_closed', '=', False),
        ])

        for lead in leads:
            try:
                created = lead._schedule_manual_stage_review(
                    summary='Review for manual Data Collection move',
                    note=(
                        f'No reply after {threshold_days}+ days of outreach. '
                        f'Review this lead manually and move it to Data Collection only if needed.'
                    ),
                )
                if not created:
                    continue
                lead.message_post(
                    body=Markup(
                        f'<b>Manual review needed</b> — '
                        f'no reply after {threshold_days}+ days of outreach. '
                        f'The stage was left unchanged so you can decide whether to move it to Data Collection.'
                    ),
                    subtype_xmlid='mail.mt_note',
                )
                _logger.info('CRM: lead %s flagged for manual Data Collection review (%d-day no-reply)', lead.id, threshold_days)
            except Exception as exc:
                _logger.warning('CRM: outreach stale manual review failed lead %s: %s', lead.id, exc)

    # ── Replied-stage follow-up timers ────────────────────────────────────────

    @api.model
    def run_replied_warning_cron(self):
        """Day +3 after customer reply: create warning activity if still in Replied stage."""
        replied = self.env['crm.stage'].sudo().search([('name', '=', 'Replied')], limit=1)
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        if not replied or not todo:
            return
        cutoff = fields.Datetime.now() - timedelta(days=3)
        # PHASE 8 — read last_meaningful_reply_at (new canonical field),
        # with the legacy studio field as fallback for pre-migration rows.
        leads = self.sudo().search([
            ('active', '=', True),
            ('stage_id', '=', replied.id),
            '|',
            ('last_meaningful_reply_at', '<=', cutoff),
            '&',
            ('last_meaningful_reply_at', '=', False),
            ('x_reply_received_at', '<=', cutoff),
        ])
        for lead in leads:
            existing = self.env['mail.activity'].search([
                ('res_model', '=', 'crm.lead'),
                ('res_id', '=', lead.id),
                ('summary', '=', 'Write follow-up reply'),
                ('date_deadline', '>=', fields.Date.today()),
            ], limit=1)
            if existing:
                continue
            try:
                lead.activity_schedule(
                    activity_type_id=todo.id,
                    summary='Write follow-up reply',
                    note='Customer replied 3+ days ago — write a follow-up now.',
                    date_deadline=fields.Date.today(),
                    user_id=lead.user_id.id or self.env.uid,
                )
                _logger.info('CRM: warning activity created for lead %s (%s)', lead.id, lead.name)
            except Exception as e:
                _logger.warning('CRM: warning activity failed for lead %s: %s', lead.id, e)

    @api.model
    def run_replied_stale_cron(self):
        """Day +6 after customer reply with no outgoing response: flag for manual review."""
        replied = self.env['crm.stage'].sudo().search([('name', '=', 'Replied')], limit=1)
        if not replied:
            return
        cutoff = fields.Datetime.now() - timedelta(days=6)
        # PHASE 8 — same dual-field fallback as the +3-day warning cron.
        leads = self.sudo().search([
            ('active', '=', True),
            ('stage_id', '=', replied.id),
            '|',
            ('last_meaningful_reply_at', '<=', cutoff),
            '&',
            ('last_meaningful_reply_at', '=', False),
            ('x_reply_received_at', '<=', cutoff),
        ])
        for lead in leads:
            try:
                created = lead._schedule_manual_stage_review(
                    summary='Review replied lead for manual Data Collection move',
                    note='Customer replied 6+ days ago with no follow-up sent. Review manually and move to Data Collection only if needed.',
                )
                if not created:
                    continue
                lead.message_post(
                    body=Markup('<b>⚠️ Manual review needed</b> — 6 days since customer reply with no follow-up sent. The stage was left unchanged for manual review.'),
                    subtype_xmlid='mail.mt_note',
                )
                _logger.info('CRM: lead %s flagged for manual Data Collection review (6-day stale)', lead.id)
            except Exception as e:
                _logger.warning('CRM: stale manual review failed for lead %s: %s', lead.id, e)
