"""
CRM AI Sales Assistant — core brain for PremaFirm's CRM bot.
Provides: AI chat widget, account summary, Won/Lost debrief + checklist,
          auto-log split (company vs contact), reply detection + outreach stamping.

FIX LOG (May 13 2026):
  - action_ai_compose_email: added default_subject, fixed default_res_id (singular),
    added default_partner_ids so compose window is properly threaded to lead.
  - _ai_system_prompt: instructed AI not to include subject line in email body drafts.
"""
import logging
import re
from datetime import date, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

PREMAFIRM_FALLBACK = (
    "PREMAFIRM INC. — Owner Operator: Ahmad Ibrahim\n"
    "Equipment: 26FT Freightliner M2 straight truck, reefer and dry capability, up to 12 pallets\n"
    "Base: Mississauga, Ontario, Canada\n"
    "Lanes: GTA, Ontario, Quebec, cross-country Canada, Canada–USA cross-border\n"
    "Federally authorized carrier (TC Canada + FMCSA compliant)\n"
    "USDOT: 4512323 | MC: 1786607 | CVOR: 227-065-594 | SCAC: PSHL\n"
    "Insurance: $2M liability / $100K cargo / Reefer breakdown included\n"
    "Services: LTL, dedicated, expedited, temperature-controlled, food-grade, produce, general freight\n"
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


def _strip_ai_meta(text):
    """Remove subject lines and signature blocks the AI accidentally writes.

    Strips:
    - Lines starting with "Subject:"
    - Everything from a signature separator (--) onward
    - Everything from a closing line (Best regards / Sincerely / Ahmad Ibrahim etc.) onward
    """
    lines = text.replace('\r\n', '\n').split('\n')
    body_lines = []
    sig_triggers = re.compile(
        r'^(--|best regards|best,|sincerely|warm regards|regards,|'
        r'ahmad ibrahim|premafirm|owner.?operator|cheers,)',
        re.IGNORECASE,
    )
    for line in lines:
        stripped = line.strip()
        if re.match(r'^subject\s*:', stripped, re.IGNORECASE):
            continue
        if sig_triggers.match(stripped):
            break
        body_lines.append(line)
    return '\n'.join(body_lines).strip()


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

    # ── FIXED: Compose Email ──────────────────────────────────────────────────

    def action_ai_compose_email(self):
        """
        Open the Odoo email composer pre-filled with the AI draft response.

        FIXES applied (May 13 2026):
          1. default_subject now populated from lead name / contact name.
          2. default_res_id (singular) used alongside default_res_ids so Odoo
             correctly threads the message to the CRM lead chatter.
          3. default_partner_ids pre-fills the To field with the lead contact.
          4. AI body is stripped of any accidental subject lines the AI may have
             written (lines starting with "Subject:").
        """
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response:
            return {'type': 'ir.actions.client', 'tag': 'reload'}

        # ── Parse SUBJECT: line from AI output ───────────────────────────────
        subject = ''
        body_lines = []
        for line in response.replace('\r\n', '\n').split('\n'):
            if not subject and re.match(r'^subject\s*:', line.strip(), re.IGNORECASE):
                subject = re.sub(r'^subject\s*:\s*', '', line.strip(), flags=re.IGNORECASE).strip()
            else:
                body_lines.append(line)
        response = '\n'.join(body_lines).strip()

        # ── Strip any remaining AI narration / signature ──────────────────────
        response = _strip_ai_meta(response)

        # ── Fallback subject if AI didn't produce one ─────────────────────────
        if not subject:
            partner = self.partner_id
            company = partner.parent_id if (partner and partner.parent_id) else (
                partner if (partner and partner.is_company) else None
            )
            company_name = company.name if company else (partner.name if partner else self.partner_name or '')
            subject = f"Carrier Introduction – {company_name}" if company_name else "PREMAFIRM INC. – Carrier Introduction"

        # ── Convert plain text to HTML ────────────────────────────────────────
        html_body = response.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html_body = '<br/>'.join(html_body.replace('\r\n', '\n').split('\n'))

        # ── Append Business Profile signature ────────────────────────────────
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            sig_text = (profile.email_signature or '').strip()
        except Exception:
            sig_text = ''

        if sig_text:
            sig_html = sig_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            sig_html = '<br/>'.join(sig_html.replace('\r\n', '\n').split('\n'))
            html_body = f'{html_body}<br/><br/>--<br/>{sig_html}'

        # ── Collect recipient partner IDs ─────────────────────────────────────
        partner = self.partner_id
        partner_ids = [partner.id] if partner and partner.id else []

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_model':            'crm.lead',
                'default_res_ids':          [self.id],
                'default_composition_mode': 'comment',
                'default_subject':          subject,
                'default_body':             html_body,
                'default_partner_ids':      partner_ids,
                'force_email':              True,
                'mark_so_as_sent':          True,
                'mail_add_signature':       False,
            },
        }

    # ── Append to Company Notes ───────────────────────────────────────────────

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
                    # Outgoing: stamp outreach time, clear attention flag
                    self.sudo().write({
                        'x_last_outreach_at': fields.Datetime.now(),
                        'x_needs_attention': False,
                    })
                    self._maybe_advance_on_outgoing()
                    self._maybe_schedule_followup_activity()
                elif result.author_id and not result.author_id.user_ids:
                    # Incoming: customer replied
                    self.sudo().write({
                        'x_response_status': 'replied',
                        'x_needs_attention': True,
                        'x_reply_received_at': fields.Datetime.now(),
                    })
                    self._auto_log_reply(result)
        except Exception as exc:
            _logger.debug('message_post tracking error lead %s: %s', self.id, exc)
        return result

    def _maybe_advance_on_outgoing(self):
        """Outreach → Contacted on first email send; Replied → Onboarding."""
        stage_name = self.stage_id.name if self.stage_id else ''
        if stage_name == 'Outreach':
            contacted = self.env['crm.stage'].sudo().search([('name', '=', 'Contacted')], limit=1)
            if contacted:
                self.sudo().write({'stage_id': contacted.id})
        elif stage_name == 'Replied':
            onboarding = self.env['crm.stage'].sudo().search([('name', '=', 'Onboarding')], limit=1)
            if onboarding:
                self.sudo().write({'stage_id': onboarding.id})

    def _maybe_schedule_followup_activity(self):
        """Create a follow-up To-Do activity only when emailing from the Outreach stage."""
        outreach = self.env['crm.stage'].sudo().search([('name', '=', 'Outreach')], limit=1)
        if not outreach or self.stage_id.id != outreach.id:
            return
        # Dedup: skip if an open follow-up activity already exists
        existing = self.env['mail.activity'].search([
            ('res_model', '=', 'crm.lead'),
            ('res_id', '=', self.id),
            ('summary', '=', 'Follow up if no reply'),
            ('date_deadline', '>=', fields.Date.today()),
        ], limit=1)
        if existing:
            return
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        if not todo:
            return
        self.activity_schedule(
            activity_type_id=todo.id,
            summary='Follow up if no reply',
            date_deadline=fields.Date.today() + timedelta(days=4),
            user_id=self.user_id.id or self.env.uid,
        )

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
        """
        Build the master system prompt for all AI operations on this lead.

        Key rules enforced here:
          - Never auto-send anything
          - No subject line in email body (Odoo handles subject separately)
          - No signature in email body (Odoo appends from user profile)
          - Concise freight-industry tone
          - Full PREMAFIRM company context always included
        """
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
            '\n\n'
            '=== EMAIL DRAFT OUTPUT FORMAT (STRICT — NO EXCEPTIONS) ===\n'
            'When asked to draft an email, your ENTIRE response must follow this exact format:\n'
            'SUBJECT: (a relevant freight sales subject line based on the company and context)\n'
            '(email body starting directly with Hi FirstName,)\n'
            '\n'
            'Rules:\n'
            '  1. First line must be SUBJECT: followed by the subject. Example:\n'
            '     SUBJECT: Reefer Carrier Introduction – PREMAFIRM INC.\n'
            '  2. Second line onward is the email body. Start directly with Hi [FirstName],\n'
            '  3. NO preamble. NO narration. NO explanation before or after the email.\n'
            '     Never write "Here is a draft...", "I understand...", "Let\'s reach out...",\n'
            '     "Sure, here\'s an email...", or ANY meta-commentary of any kind.\n'
            '  4. NO signature block — it is appended automatically.\n'
            '  5. Keep body under 120 words unless specifically asked for more.\n'
            '  6. Professional freight-industry tone — direct, warm, no hollow filler phrases.\n'
            '  7. Never auto-send. All output is for Ahmad to review first.\n'
            + (f'\nSEASONAL NOTE: {seasonal}' if seasonal else '')
        )

    def _ai_lead_context(self):
        """
        Build a rich context string from the lead for AI consumption.
        Reads: lead fields, email thread (newest first), contact notes, company notes.
        All three sources are merged so the AI has full account memory.
        """
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

        # Email thread (newest first, last 8 messages)
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

        # Contact-level notes (personal preferences, tone, communication style)
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

        # Company-level notes (freight requirements, lane history, rate discussions, escalations)
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
        """
        Generate a structured account summary for a company partner.
        Reads: contact list, all linked leads (stage + last activity),
               company log notes (last 15 entries).
        Output sections: STATUS | PRIMARY CONTACT | LANE INTERESTS |
                         LAST ACTIVITY | NEXT ACTION | RISK FLAGS | OPPORTUNITY SCORE.
        Summary is stored in x_account_summary field on the company record.
        """
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
                parts.append(
                    f'  [{l.stage_id.name if l.stage_id else "?"}] {l.name} '
                    f'— last contact: {ts} — status: {l.x_response_status or "?"}'
                )

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
