"""
CRM AI Sales Assistant — core brain for PremaFirm's CRM bot.
Provides: AI chat widget, account summary, Won/Lost debrief + checklist,
          auto-log split (company vs contact), reply detection + outreach stamping.
"""
import logging
import re
from datetime import date

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

PREMAFIRM_FALLBACK = (
    "PREMAFIRM INC. — Owner Operator: Ahmad Ibrahim\n"
    "Equipment: 26FT Freightliner M2 straight truck, reefer and dry capability, up to 26 pallets\n"
    "Base: Mississauga, Ontario, Canada\n"
    "Lanes: GTA, Ontario, Quebec, cross-country Canada, Canada–USA cross-border\n"
    "Federally authorized carrier (TC Canada + FMCSA compliant)\n"
    "Services: FTL, temperature-controlled freight, food-grade, produce, general freight\n"
    "Ahmad drives the truck himself and manages all sales alone — he needs concise, actionable help."
)

SEASONAL = {
    3: "Produce season approaching — food distributors and grocers need reefer capacity.",
    4: "Peak produce season — temperature-controlled freight demand is high.",
    5: "Peak produce season — prioritize food/produce leads for reefer lanes.",
    6: "Late produce season — still strong for reefer.",
    10: "Pre-holiday surge — retailers and distributors building inventory.",
    11: "Holiday freight peak — high demand across all lanes.",
}


def _api_key(env):
    return (env['ir.config_parameter'].sudo().get_param('openai.api_key') or '').strip()


def _strip_html(html):
    text = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', text).strip()


def _gpt(env, system, messages, max_tokens=800):
    from odoo.addons.premafirm_ai_engine.services.openai_utils import openai_chat
    key = _api_key(env)
    if not key:
        raise ValueError("OpenAI API key not configured.")
    return openai_chat(messages=messages, system=system, max_tokens=max_tokens, api_key=key)


# ── CRM Lead AI Assistant ─────────────────────────────────────────────────────

class CrmLeadAIAssistant(models.Model):
    _inherit = 'crm.lead'

    x_ai_chat_input = fields.Text(
        string='Ask AI',
        help='Type your question or request, then click Ask AI.',
    )
    x_ai_chat_response = fields.Text(string='AI Response', readonly=True)
    x_followup_1_sent_at = fields.Datetime(string='Follow-up 1 Drafted')
    x_followup_2_sent_at = fields.Datetime(string='Follow-up 2 Drafted')

    # ── Chat ─────────────────────────────────────────────────────────────────

    def action_ai_chat_send(self):
        self.ensure_one()
        user_input = (self.x_ai_chat_input or '').strip()
        if not user_input:
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        try:
            system = self._ai_system_prompt()
            context = self._ai_lead_context()
            response = _gpt(self.env, system, [{
                'role': 'user',
                'content': f'ACCOUNT CONTEXT:\n{context}\n\nREQUEST:\n{user_input}',
            }], max_tokens=1200)
            self.sudo().write({'x_ai_chat_response': response})
        except Exception as exc:
            self.sudo().write({'x_ai_chat_response': f'⚠ {exc}'})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ai_compose_email(self):
        """Open Odoo email composer pre-filled with the AI response."""
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response:
            return {'type': 'ir.actions.client', 'tag': 'reload'}

        # Convert plain text to HTML, preserve paragraph breaks
        html_body = response.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html_body = '<br/>'.join(html_body.replace('\r\n', '\n').split('\n'))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_model': 'crm.lead',
                'default_res_ids': [self.id],
                'default_composition_mode': 'comment',
                'default_body': html_body,
                'force_email': True,
            },
        }

    def action_ai_append_company(self):
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response:
            return
        company = self._ai_company_partner()
        if company:
            company.message_post(
                body=f'<b>[AI — Lead #{self.id}]</b><br/>{response.replace(chr(10), "<br/>")}',
                subtype_xmlid='mail.mt_note',
            )
        self.sudo().write({'x_ai_chat_response': ''})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ai_append_contact(self):
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response or not self.partner_id:
            return
        self.partner_id.message_post(
            body=f'<b>[AI — Lead #{self.id}]</b><br/>{response.replace(chr(10), "<br/>")}',
            subtype_xmlid='mail.mt_note',
        )
        self.sudo().write({'x_ai_chat_response': ''})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ai_append_lead(self):
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response:
            return
        self.message_post(
            body=f'<b>[AI]</b> {response.replace(chr(10), "<br/>")}',
            subtype_xmlid='mail.mt_note',
        )
        self.sudo().write({'x_ai_chat_response': ''})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ── Won / Lost Debrief ────────────────────────────────────────────────────

    def action_set_won(self):
        result = super().action_set_won()
        for lead in self:
            try:
                lead._ai_won_debrief()
            except Exception as exc:
                _logger.warning('AI won debrief failed lead %s: %s', lead.id, exc)
        return result

    def action_set_lost(self, **kw):
        result = super().action_set_lost(**kw)
        for lead in self:
            try:
                lead._ai_lost_debrief()
            except Exception as exc:
                _logger.warning('AI lost debrief failed lead %s: %s', lead.id, exc)
        return result

    def _ai_won_debrief(self):
        company = self._ai_company_partner()
        context = self._ai_lead_context()
        debrief = ''
        try:
            debrief = _gpt(self.env,
                'You are a freight sales account manager. Write a 3-sentence won-deal debrief: what worked, what was agreed, next step.',
                [{'role': 'user', 'content': f'Lead just marked WON:\n{context}'}], max_tokens=250)
        except Exception:
            pass

        if company:
            company.message_post(
                body=(
                    f'<b>✅ WON — Lead #{self.id}: {self.name}</b><br/>'
                    + (debrief.replace('\n', '<br/>') if debrief else '')
                ),
                subtype_xmlid='mail.mt_note',
            )
        self.message_post(
            body=(
                '<b>🎉 Carrier Onboarding Checklist</b><br/>'
                '☐ Carrier packet sent<br/>'
                '☐ Insurance certificate received<br/>'
                '☐ CVOR + FMCSA (if cross-border) verified<br/>'
                '☐ Customer portal login created<br/>'
                '☐ First load date confirmed<br/>'
                '☐ Rate confirmation signed<br/>'
                '☐ BOL template shared<br/>'
                '☐ Dispatch + after-hours contact verified<br/>'
            ),
            subtype_xmlid='mail.mt_note',
        )

    def _ai_lost_debrief(self):
        company = self._ai_company_partner()
        reason = ''
        if hasattr(self, 'lost_reason_id') and self.lost_reason_id:
            reason = self.lost_reason_id.name
        context = self._ai_lead_context()
        debrief = ''
        try:
            debrief = _gpt(self.env,
                'You are a freight sales account manager. Write a 2-sentence lost-deal debrief: what the objection was, what to improve.',
                [{'role': 'user', 'content': f'Lead marked LOST (reason: {reason or "unspecified"}):\n{context}'}],
                max_tokens=200)
        except Exception:
            pass

        if company:
            company.message_post(
                body=(
                    f'<b>❌ LOST — Lead #{self.id}: {self.name}</b><br/>'
                    f'Reason: {reason or "Not specified"}<br/>'
                    + (debrief.replace('\n', '<br/>') if debrief else '')
                ),
                subtype_xmlid='mail.mt_note',
            )

    # ── Reply detection + outreach stamping ──────────────────────────────────

    def message_post(self, **kwargs):
        result = super().message_post(**kwargs)
        if not result:
            return result
        try:
            if result.message_type == 'email':
                if result.author_id and result.author_id.user_ids:
                    # Outgoing — stamp outreach date
                    self.sudo().write({'x_last_outreach_at': fields.Datetime.now()})
                elif result.author_id and not result.author_id.user_ids:
                    # Incoming reply from external contact
                    self.sudo().write({'x_response_status': 'replied'})
                    # Log to company and contact automatically
                    self._auto_log_reply(result)
        except Exception as exc:
            _logger.debug('message_post tracking error lead %s: %s', self.id, exc)
        return result

    def _auto_log_reply(self, message):
        """Auto-log incoming reply summary to company and contact records."""
        body = _strip_html(message.body)
        if not body or len(body) < 15:
            return
        author = message.author_id.name if message.author_id else 'External contact'
        snippet = body[:300]
        note = (
            f'<b>↩ Reply received from {author}</b> on Lead #{self.id}<br/>'
            f'{snippet}'
        )
        company = self._ai_company_partner()
        if company:
            company.message_post(body=note, subtype_xmlid='mail.mt_note')
        if self.partner_id and not self.partner_id.is_company:
            self.partner_id.message_post(body=note, subtype_xmlid='mail.mt_note')

    # ── Context builders ──────────────────────────────────────────────────────

    def _ai_company_partner(self):
        p = self.partner_id
        if not p:
            return None
        return p.parent_id if p.parent_id else (p if p.is_company else None)

    def _ai_system_prompt(self):
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            base = profile.get_system_prompt()
        except Exception:
            base = PREMAFIRM_FALLBACK
        seasonal = SEASONAL.get(date.today().month, '')
        return (
            base + '\n\n'
            '=== YOUR ROLE ===\n'
            'You are Ahmad Ibrahim\'s personal AI freight sales assistant inside Odoo CRM. '
            'Ahmad drives the truck and manages all sales alone — he is extremely busy. '
            'Think like a senior freight sales account manager. '
            'Be concise, direct, and immediately useful. Give Ahmad exactly what he needs — no fluff. '
            'When drafting emails: write them ready-to-send, professional, freight-industry tone, under 120 words unless asked for more. '
            'Do NOT include a signature in email drafts — Odoo adds it automatically from the user profile. '
            'Never auto-send anything. All drafts are for Ahmad to review first.'
            + (f'\nSEASONAL NOTE: {seasonal}' if seasonal else '')
        )

    def _ai_lead_context(self):
        parts = []
        partner = self.partner_id
        company = self._ai_company_partner()
        contact_name = partner.name if partner else self.partner_name or 'Unknown'
        company_name = company.name if company else contact_name

        parts.append('--- LEAD ---')
        parts.append(f'Lead #{self.id}: {self.name}')
        parts.append(f'Contact: {contact_name}' + (f' | {partner.function}' if partner and partner.function else ''))
        parts.append(f'Company: {company_name}')
        if partner and partner.email:
            parts.append(f'Email: {partner.email}')
        if partner and partner.phone:
            parts.append(f'Phone: {partner.phone}')
        if self.stage_id:
            parts.append(f'Stage: {self.stage_id.name}')
        if self.tag_ids:
            parts.append(f'Tags: {", ".join(self.tag_ids.mapped("name"))}')
        if self.x_response_status:
            parts.append(f'Response: {self.x_response_status}')
        if self.x_last_outreach_at:
            parts.append(f'Last outreach: {self.x_last_outreach_at.strftime("%Y-%m-%d")}')
        if self.x_rotation_count:
            parts.append(f'Contacts tried: {self.x_rotation_count}')

        # Email thread
        parts.append('\n--- EMAIL THREAD (newest first) ---')
        count = 0
        for msg in self.message_ids.sorted('date', reverse=True):
            if msg.message_type not in ('email', 'comment'):
                continue
            body = _strip_html(msg.body)
            if not body or len(body) < 10:
                continue
            direction = '→ SENT' if (msg.author_id and msg.author_id.user_ids) else '← RECEIVED'
            ts = msg.date.strftime('%Y-%m-%d') if msg.date else '??'
            author = msg.author_id.name if msg.author_id else '?'
            parts.append(f'[{direction} | {ts} | {author}]: {body[:400]}')
            count += 1
            if count >= 8:
                break

        # Contact notes
        if partner and not partner.is_company:
            cnotes = partner.message_ids.filtered(
                lambda m: m.message_type == 'comment'
            ).sorted('date', reverse=True)[:4]
            if cnotes:
                parts.append('\n--- CONTACT NOTES ---')
                for n in cnotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{n.date.strftime("%Y-%m-%d") if n.date else "??"}]: {b[:300]}')

        # Company notes
        if company:
            anotes = company.message_ids.filtered(
                lambda m: m.message_type == 'comment'
            ).sorted('date', reverse=True)[:4]
            if anotes:
                parts.append('\n--- COMPANY NOTES ---')
                for n in anotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{n.date.strftime("%Y-%m-%d") if n.date else "??"}]: {b[:300]}')

        return '\n'.join(parts)


# ── Partner Account Summary ───────────────────────────────────────────────────

class ResPartnerAI(models.Model):
    _inherit = 'res.partner'

    x_account_summary = fields.Text(
        string='AI Account Summary',
        readonly=True,
        help='AI-generated account summary. Click "Generate Summary" to refresh.',
    )

    def action_generate_account_summary(self):
        self.ensure_one()
        parts = [f'Company: {self.name}']
        if self.website:
            parts.append(f'Website: {self.website}')
        if self.city:
            parts.append(f'City: {self.city}')
        if self.phone:
            parts.append(f'Phone: {self.phone}')

        contacts = self.child_ids.filtered(lambda p: not p.is_company and p.active)
        if contacts:
            parts.append(f'\nContacts ({len(contacts)}):')
            for c in contacts[:8]:
                parts.append(f'  {c.name} — {c.function or "No title"} — {c.email or "No email"}')

        leads = self.env['crm.lead'].sudo().search([
            '|', ('partner_id', '=', self.id), ('partner_id.parent_id', '=', self.id)
        ])
        if leads:
            parts.append(f'\nLeads ({len(leads)}):')
            for l in leads[:8]:
                ts = l.x_last_outreach_at.strftime('%Y-%m-%d') if l.x_last_outreach_at else 'never'
                parts.append(f'  [{l.stage_id.name if l.stage_id else "?"}] {l.name} — last contact: {ts} — status: {l.x_response_status or "?"}')

        notes = self.message_ids.filtered(
            lambda m: m.message_type == 'comment'
        ).sorted('date', reverse=True)[:15]
        if notes:
            parts.append('\nLog Notes:')
            for n in notes:
                b = _strip_html(n.body)
                if b and len(b) > 10:
                    parts.append(f'[{n.date.strftime("%Y-%m-%d") if n.date else "??"}]: {b[:400]}')

        context_text = '\n'.join(parts)[:3500]

        try:
            summary = _gpt(
                self.env,
                (
                    'You are a senior freight sales account manager for PremaFirm Inc. '
                    'Generate a structured account summary with these sections: '
                    'STATUS | PRIMARY CONTACT | LANE INTERESTS | LAST ACTIVITY | '
                    'NEXT ACTION | RISK FLAGS | OPPORTUNITY SCORE (1-10). '
                    'Be concise. Plain text, no markdown.'
                ),
                [{'role': 'user', 'content': f'Generate account summary:\n{context_text}'}],
                max_tokens=500,
            )
            self.sudo().write({'x_account_summary': summary})
        except Exception as exc:
            self.sudo().write({'x_account_summary': f'⚠ {exc}'})

        return {'type': 'ir.actions.client', 'tag': 'reload'}
