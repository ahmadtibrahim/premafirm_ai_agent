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

from markupsafe import Markup
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
    from odoo.addons.premafirm_ai_engine.services.deepseek_utils import get_api_key as _get_deepseek_key
    return _get_deepseek_key(env)


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
    from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat
    key = _api_key(env)
    if not key:
        raise ValueError("DeepSeek API key not configured.")
    return deepseek_chat(messages=messages, system=system, max_tokens=max_tokens, api_key=key)


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

        # ── Find SUBJECT line — discard everything before it (analysis notes) ──
        subject = ''
        lines = response.replace('\r\n', '\n').split('\n')
        email_start = None
        for i, line in enumerate(lines):
            if re.match(r'^subject\s*:', line.strip(), re.IGNORECASE):
                subject = re.sub(r'^subject\s*:\s*', '', line.strip(), flags=re.IGNORECASE).strip()
                email_start = i + 1
                break

        if email_start is not None:
            # Take only the email body (lines after SUBJECT:)
            response = '\n'.join(lines[email_start:]).strip()
        else:
            # No SUBJECT line — look for a --- separator; take everything after it
            sep_idx = next(
                (i for i, ln in enumerate(lines) if re.match(r'^-{3,}\s*$', ln.strip())),
                None,
            )
            response = '\n'.join(lines[sep_idx + 1:] if sep_idx is not None else lines).strip()

        # ── Strip any remaining signature blocks ──────────────────────────────
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

        # ── Append current user's signature ──────────────────────────────────
        user_sig = (self.env.user.signature or '').strip()
        if not user_sig:
            # Fall back to business profile signature if user has none set
            try:
                profile = self.env['premafirm.business.profile'].sudo().get_profile()
                sig_text = (profile.email_signature or '').strip()
                if sig_text:
                    user_sig = '<br/>'.join(
                        sig_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        .replace('\r\n', '\n').split('\n')
                    )
            except Exception:
                pass

        if user_sig:
            html_body = f'{html_body}<br/><br/>--<br/>{user_sig}'

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
                # PHASE 9 — the composer creates ONE mail.mail; the
                # mail_send_hooks create hook stamps AI provenance on it.
                'premafirm_ai_origin':      'chat_compose',
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
            safe_resp = Markup.escape(response).replace('\n', Markup('<br/>'))
            company.message_post(
                body=Markup(f'<b>[AI — Lead #{self.id}]</b><br/>') + safe_resp,
                subtype_xmlid='mail.mt_note',
            )
        self.sudo().write({'x_ai_chat_response': ''})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ai_append_contact(self):
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response or not self.partner_id:
            return
        safe_resp = Markup.escape(response).replace('\n', Markup('<br/>'))
        self.partner_id.message_post(
            body=Markup(f'<b>[AI — Lead #{self.id}]</b><br/>') + safe_resp,
            subtype_xmlid='mail.mt_note',
        )
        self.sudo().write({'x_ai_chat_response': ''})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ai_append_lead(self):
        self.ensure_one()
        response = (self.x_ai_chat_response or '').strip()
        if not response:
            return
        safe_resp = Markup.escape(response).replace('\n', Markup('<br/>'))
        self.message_post(
            body=Markup('<b>[AI]</b> ') + safe_resp,
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
        from odoo.addons.premafirm_ai_engine.models.business_profile import DEFAULT_WON_DEBRIEF_PROMPT
        company = self._ai_company_partner()
        context = self._ai_lead_context()
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            won_prompt = profile.ai_won_debrief_prompt or DEFAULT_WON_DEBRIEF_PROMPT
        except Exception:
            won_prompt = DEFAULT_WON_DEBRIEF_PROMPT
        debrief = ''
        try:
            debrief = _gpt(self.env,
                won_prompt,
                [{'role': 'user', 'content': f'Lead just marked WON:\n{context}'}], max_tokens=250)
        except Exception:
            pass

        if company:
            debrief_html = Markup.escape(debrief).replace('\n', Markup('<br/>')) if debrief else Markup('')
            company.message_post(
                body=Markup(f'<b>✅ WON — Lead #{self.id}: {Markup.escape(self.name or "")}</b><br/>') + debrief_html,
                subtype_xmlid='mail.mt_note',
            )
        self.message_post(
            body=Markup(
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
        from odoo.addons.premafirm_ai_engine.models.business_profile import DEFAULT_LOST_DEBRIEF_PROMPT
        company = self._ai_company_partner()
        reason = ''
        if hasattr(self, 'lost_reason_id') and self.lost_reason_id:
            reason = self.lost_reason_id.name
        context = self._ai_lead_context()
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            lost_prompt = profile.ai_lost_debrief_prompt or DEFAULT_LOST_DEBRIEF_PROMPT
        except Exception:
            lost_prompt = DEFAULT_LOST_DEBRIEF_PROMPT
        debrief = ''
        try:
            debrief = _gpt(self.env,
                lost_prompt,
                [{'role': 'user', 'content': f'Lead marked LOST (reason: {reason or "unspecified"}):\n{context}'}],
                max_tokens=200)
        except Exception:
            pass

        if company:
            debrief_html = Markup.escape(debrief).replace('\n', Markup('<br/>')) if debrief else Markup('')
            company.message_post(
                body=(
                    Markup(f'<b>❌ LOST — Lead #{self.id}: {Markup.escape(self.name or "")}</b><br/>'
                           f'Reason: {Markup.escape(reason or "Not specified")}<br/>') + debrief_html
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
                    # Issue 13 — rule-4 guard: only a genuine outbound
                    # customer email (at least one EXTERNAL recipient)
                    # may advance NEW / UNCONTACTED → OUTREACH SENT;
                    # internal-only emails never move the stage.
                    update_stage = self._outreach_has_external_recipient(result)
                    self._mark_outbound_activity(update_stage=update_stage)
                elif result.author_id and not result.author_id.user_ids:
                    # Incoming: customer replied
                    self.sudo().write({
                        'x_response_status': 'replied',
                        'x_needs_attention': True,
                        'x_attention_at': fields.Datetime.now(),
                        'x_attention_reason': 'reply',
                        'x_reply_received_at': fields.Datetime.now(),
                    })
                    self._auto_log_reply(result)
            elif self._is_internal_note_activity(result):
                self._mark_outbound_activity(update_stage=False)
            # PHASE 41 — any new thread message can move the wait-queue
            # timestamp; the compute itself filters system noise.
            self.env.add_to_compute(
                self._fields['x_meaningful_activity_at'], self)
        except Exception as exc:
            _logger.debug('message_post tracking error lead %s: %s', self.id, exc)
        return result

    def _maybe_advance_on_outgoing(self):
        """Route the FIRST outbound email from a fresh lead into OUTREACH SENT.

        Canonical-pipeline behavior only: a lead in NEW / UNCONTACTED that
        just received its first outbound email moves to OUTREACH SENT.
        Every other stage (ENGAGED / REPLIED, QUALIFIED / DATA COLLECTED,
        QUOTE*, NEGOTIATION, ONBOARDING, WON, LOST, PAUSED) is left
        untouched — the legacy version searched the archived "Contacted" /
        "Onboarding" stage names by raw name lookup, which silently moved
        leads INTO the folded legacy stages.
        """
        if self._normalized_stage_name() != 'new / uncontacted':
            return
        targets = self._premafirm_target_stages()
        outreach = targets.get('outreach sent')
        if outreach:
            self.sudo().write({'stage_id': outreach.id})

    def _maybe_schedule_followup_activity(self):
        """Create a follow-up To-Do activity after outbound outreach."""
        if self._normalized_stage_name() not in {'new / uncontacted', 'outreach sent'}:
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

    def _mark_outbound_activity(self, update_stage):
        self.ensure_one()
        self.sudo().write({
            'x_last_outreach_at': fields.Datetime.now(),
            'x_needs_attention': False,
            'x_attention_at': False,
            'x_attention_reason': False,
        })
        if update_stage:
            self._maybe_advance_on_outgoing()
            self._maybe_schedule_followup_activity()

    def _is_internal_note_activity(self, message):
        if message.message_type != 'comment':
            return False
        if not (message.author_id and message.author_id.user_ids):
            return False
        if message.tracking_value_ids:
            return False
        return bool(_strip_html(message.body))

    def _normalized_stage_name(self):
        return (self.stage_id.name or '').strip().lower() if self.stage_id else ''

    def _auto_log_reply(self, message):
        """Auto-log incoming reply summary to company and contact records."""
        body = _strip_html(message.body)
        if not body or len(body) < 15:
            return
        author = message.author_id.name if message.author_id else 'External contact'
        snippet = body[:300]
        note = (
            Markup(f'<b>↩ Reply received from {Markup.escape(author)}</b> on Lead #{self.id}<br/>')
            + Markup.escape(snippet)
        )
        company = self._ai_company_partner()
        if company:
            company.message_post(body=note, subtype_xmlid='mail.mt_note')
        if self.partner_id and not self.partner_id.is_company:
            self.partner_id.message_post(body=note, subtype_xmlid='mail.mt_note')

    # ── Reply to specific incoming email ──────────────────────────────────────

    def action_reply_last_email(self):
        """
        Open the compose window pre-threaded to the most recent incoming email.
        Sets parent_id so Odoo writes the correct In-Reply-To / References headers,
        preserving the existing email subject and thread in the recipient's mail client.
        """
        self.ensure_one()
        incoming = self.message_ids.filtered(
            lambda m: m.message_type == 'email'
            and m.author_id
            and not m.author_id.user_ids
        ).sorted('date', reverse=True)

        if not incoming:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'mail.compose.message',
                'view_mode': 'form',
                'target': 'new',
                'context': {
                    'default_model': 'crm.lead',
                    'default_res_ids': [self.id],
                    'default_composition_mode': 'comment',
                    'active_id': self.id,
                    'active_model': 'crm.lead',
                },
            }

        last_msg = incoming[0]

        orig_subject = (last_msg.subject or '').strip()
        if orig_subject and not orig_subject.lower().startswith('re:'):
            reply_subject = f'Re: {orig_subject}'
        else:
            reply_subject = orig_subject or f'Re: {self.name}'

        partner_ids = [last_msg.author_id.id] if last_msg.author_id else []

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_model': 'crm.lead',
                'default_res_ids': [self.id],
                'default_parent_id': last_msg.id,
                'default_composition_mode': 'comment',
                'default_subject': reply_subject,
                'default_partner_ids': partner_ids,
                'active_id': self.id,
                'active_model': 'crm.lead',
                'force_email': True,
                'mail_add_signature': False,
            },
        }

    # ── Context builders ──────────────────────────────────────────────────────

    def _ai_company_partner(self):
        p = self.partner_id
        if not p:
            return None
        return p.parent_id if p.parent_id else (p if p.is_company else None)

    def _ai_system_prompt(self):
        from odoo.addons.premafirm_ai_engine.models.business_profile import DEFAULT_ROLE_PROMPT
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            base = profile.get_system_prompt()
            role_block = profile.ai_role_prompt or DEFAULT_ROLE_PROMPT
        except Exception:
            base = PREMAFIRM_FALLBACK
            role_block = DEFAULT_ROLE_PROMPT
        seasonal = SEASONAL.get(date.today().month, '')
        prompt = base + '\n\n' + role_block
        if seasonal:
            prompt += f'\n\nSEASONAL CONTEXT: {seasonal}'
        # Identify the actual logged-in user so the AI uses the right name
        current_user = self.env.user
        if current_user and current_user.name:
            prompt += f'\n\nCURRENT USER: You are acting as {current_user.name}. Use their name in any email sign-offs or self-references, NOT Ahmad Ibrahim.'
        return prompt

    def _ai_lead_context(self):
        """Build a 360° account context for the AI: lead, contacts, activities, emails, notes, invoices, leads."""
        parts = []
        partner = self.partner_id
        company = self._ai_company_partner()
        contact_name = partner.name if partner else self.partner_name or 'Unknown'
        company_name = company.name if company else contact_name

        # ── Lead snapshot ─────────────────────────────────────────────────────
        parts.append('=== CURRENT LEAD ===')
        parts.append(f'Lead #{self.id}: {self.name}')
        parts.append(f'Contact: {contact_name}'
                     + (f' | {partner.function}' if partner and partner.function else ''))
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
            label = {'none': 'No reply yet', 'replied': 'Customer replied', 'bounced': 'Email bounced',
                     'unsubscribed': 'Unsubscribed'}.get(self.x_response_status, self.x_response_status)
            parts.append(f'Response status: {label}')
        if self.x_last_outreach_at:
            days_ago = (date.today() - self.x_last_outreach_at.date()).days
            parts.append(f'Last outreach: {self.x_last_outreach_at.strftime("%Y-%m-%d")} ({days_ago} days ago)')
        referred = getattr(self, 'x_referred_by_partner_id', None)
        if referred:
            parts.append(f'Referred by: {referred.name}'
                         + (f' ({referred.function})' if referred.function else ''))

        # ── Stage change history ───────────────────────────────────────────────
        stage_changes = []
        try:
            for msg in self.message_ids.sorted('date'):
                for tv in msg.sudo().tracking_value_ids:
                    try:
                        if tv.field_id.name == 'stage_id':
                            ts = msg.date.strftime('%Y-%m-%d') if msg.date else '?'
                            old = tv.old_value_char or '?'
                            new = tv.new_value_char or '?'
                            stage_changes.append(f'[{ts}] {old} → {new}')
                    except Exception:
                        pass
        except Exception:
            pass
        if stage_changes:
            parts.append('Stage history: ' + ' | '.join(stage_changes[-6:]))

        # ── Open activities & follow-up tasks ────────────────────────────────
        try:
            activities = self.env['mail.activity'].sudo().search([
                ('res_model', '=', 'crm.lead'), ('res_id', '=', self.id),
            ])
            if activities:
                parts.append('\n=== OPEN ACTIVITIES / FOLLOW-UP TASKS ===')
                for act in activities:
                    deadline = act.date_deadline.strftime('%Y-%m-%d') if act.date_deadline else '?'
                    state_label = {'overdue': '⚠ OVERDUE', 'today': 'DUE TODAY', 'planned': f'due {deadline}'}.get(
                        act.state, f'due {deadline}')
                    note = _strip_html(act.note or '')[:120] if act.note else ''
                    parts.append(
                        f'[{act.activity_type_id.name}] {act.summary or "(no summary)"}'
                        + (f' — {note}' if note else '')
                        + f' — {state_label} — assigned: {act.user_id.name}'
                    )
        except Exception:
            pass

        # ── Meetings / calls ─────────────────────────────────────────────────
        try:
            meetings = self.env['calendar.event'].sudo().search(
                [('opportunity_id', '=', self.id)], limit=5, order='start desc'
            )
            if meetings:
                parts.append('\n=== MEETINGS / CALLS ===')
                for m in meetings:
                    ts = m.start.strftime('%Y-%m-%d %H:%M') if m.start else '?'
                    attendees = ', '.join(m.partner_ids.mapped('name')[:4])
                    parts.append(f'[{ts}] {m.name} — attendees: {attendees}')
        except Exception:
            pass

        # ── Contact profile ───────────────────────────────────────────────────
        if partner and not partner.is_company:
            cp = []
            if partner.function:
                cp.append(f'Title: {partner.function}')
            if partner.website:
                cp.append(f'LinkedIn/Website: {partner.website}')
            if getattr(partner, 'comment', None):
                cp.append(f'Internal notes: {_strip_html(partner.comment)[:200]}')
            if cp:
                parts.append('\n=== CONTACT PROFILE ===')
                parts.extend(cp)

        # ── Company profile ───────────────────────────────────────────────────
        if company:
            cp = []
            if company.website:
                cp.append(f'Website: {company.website}')
            if company.city:
                cp.append(f'City: {company.city}')
            if getattr(company, 'industry_id', None) and company.industry_id:
                cp.append(f'Industry: {company.industry_id.name}')
            if getattr(company, 'comment', None):
                cp.append(f'Internal notes: {_strip_html(company.comment)[:200]}')
            if cp:
                parts.append('\n=== COMPANY PROFILE ===')
                parts.extend(cp)

        # ── ALL contacts at this company ──────────────────────────────────────
        if company:
            all_contacts = company.child_ids.filtered(lambda c: not c.is_company and c.active)
            if all_contacts:
                parts.append('\n=== ALL CONTACTS AT THIS COMPANY ===')
                for c in all_contacts[:12]:
                    line = f'{c.name} — {c.function or "No title"} — {c.email or "no email"}'
                    if c.phone or c.mobile:
                        line += f' — {c.phone or c.mobile}'
                    # Flag if this contact is the current lead contact
                    if partner and c.id == partner.id:
                        line += ' ← CURRENT CONTACT'
                    parts.append(line)

        # ── Email thread — newest first, last 12 ─────────────────────────────
        parts.append('\n=== EMAIL THREAD (newest first) ===')
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
            parts.append(f'[{direction} | {ts} | {author}]: {body[:500]}')
            count += 1
            if count >= 12:
                break
        if count == 0:
            parts.append('(no emails yet — this would be FIRST CONTACT)')

        # ── Contact log notes ─────────────────────────────────────────────────
        if partner and not partner.is_company:
            cnotes = [n for n in partner.message_ids.sorted('date', reverse=True)
                      if n.message_type == 'comment' and _strip_html(n.body)][:8]
            if cnotes:
                parts.append('\n=== CONTACT LOG NOTES ===')
                for n in cnotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{n.date.strftime("%Y-%m-%d") if n.date else "??"}]: {b[:400]}')

        # ── Company log notes ─────────────────────────────────────────────────
        if company:
            anotes = [n for n in company.message_ids.sorted('date', reverse=True)
                      if n.message_type == 'comment' and _strip_html(n.body)][:10]
            if anotes:
                parts.append('\n=== COMPANY LOG NOTES ===')
                for n in anotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{n.date.strftime("%Y-%m-%d") if n.date else "??"}]: {b[:400]}')

        # ── Invoice history ───────────────────────────────────────────────────
        search_partner_ids = []
        if partner:
            search_partner_ids.append(partner.id)
        if company and company.id not in search_partner_ids:
            search_partner_ids.append(company.id)
        if search_partner_ids:
            invoices = self.env['account.move'].sudo().search([
                ('partner_id', 'in', search_partner_ids),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
            ], limit=5, order='invoice_date desc')
            if not invoices and company:
                invoices = self.env['account.move'].sudo().search([
                    ('partner_id.parent_id', '=', company.id),
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'posted'),
                ], limit=5, order='invoice_date desc')
            if invoices:
                parts.append('\n=== INVOICE HISTORY (last 5 posted) ===')
                for inv in invoices:
                    lines_summary = ', '.join(
                        (l.name or (l.product_id.name if l.product_id else '') or '')[:50]
                        for l in inv.invoice_line_ids.filtered(lambda ln: not ln.display_type)[:3]
                    )
                    days_ago = (date.today() - inv.invoice_date).days if inv.invoice_date else '?'
                    parts.append(
                        f'[{inv.invoice_date}] {inv.name} — '
                        f'${inv.amount_total:,.0f} {inv.currency_id.name} — '
                        f'{lines_summary or "no line detail"} ({days_ago} days ago)'
                    )
                total_rev = sum(inv.amount_total for inv in invoices)
                parts.append(f'Total from last {len(invoices)} invoices: ${total_rev:,.0f}')

        # ── Other leads for this account ──────────────────────────────────────
        if company:
            other_leads = self.env['crm.lead'].sudo().search([
                '|',
                ('partner_id', '=', company.id),
                ('partner_id.parent_id', '=', company.id),
                ('id', '!=', self.id),
            ], limit=8, order='create_date desc')
            if other_leads:
                parts.append('\n=== OTHER LEADS / HISTORY FOR THIS ACCOUNT ===')
                for l in other_leads:
                    ts = l.x_last_outreach_at.strftime('%Y-%m-%d') if l.x_last_outreach_at else 'no outreach'
                    won_lost = ' ✅ WON' if l.active and getattr(l, 'probability', 0) == 100 else \
                               (' ❌ LOST' if not l.active else '')
                    contact_on_lead = l.partner_id.name if l.partner_id else '?'
                    parts.append(
                        f'[{l.stage_id.name if l.stage_id else "?"}]{won_lost} '
                        f'Contact: {contact_on_lead} — {l.name} — '
                        f'last outreach: {ts} — reply: {l.x_response_status or "none"}'
                    )

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

        from odoo.addons.premafirm_ai_engine.models.business_profile import DEFAULT_ACCOUNT_SUMMARY_PROMPT
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            summary_prompt = profile.ai_account_summary_prompt or DEFAULT_ACCOUNT_SUMMARY_PROMPT
        except Exception:
            summary_prompt = DEFAULT_ACCOUNT_SUMMARY_PROMPT
        try:
            summary = _gpt(
                self.env,
                summary_prompt,
                [{'role': 'user', 'content': f'Generate account summary:\n{context_text}'}],
                max_tokens=500,
            )
            self.sudo().write({'x_account_summary': summary})
        except Exception as exc:
            self.sudo().write({'x_account_summary': f'⚠ {exc}'})

        return {'type': 'ir.actions.client', 'tag': 'reload'}


# ── AI Reply Wizard ───────────────────────────────────────────────────────────

class CrmAiReplyWizard(models.TransientModel):
    _name = 'premafirm.crm.ai.reply.wizard'
    _description = 'AI Reply Wizard'

    lead_id = fields.Many2one('crm.lead', required=True, ondelete='cascade')
    user_intent = fields.Text(
        string='What do you want to say?',
        help='Write in plain language — the AI will turn it into a professional email.',
    )
    draft_body = fields.Html(string='AI Draft', readonly=True)
    # PHASE 9 — approval step: the AI draft becomes an editable final body,
    # with CC and attachments preserved from the original inbound, and the
    # prompt version recorded. Approve & Send produces ONE canonical
    # mail.mail (premafirm.mail.threading) stamped with provenance
    # (generated_by_ai / approved_by / approved_at / auto_sent=False).
    final_body = fields.Html(
        string='Final Body',
        help='Review and edit the AI draft before sending.',
    )
    cc = fields.Char(
        string='CC',
        help='Carbon copy recipients. Preserve recipients from the original '
             'email where appropriate (original inbound recipients are not '
             'stored by core — add them here if the customer was CC\'d).',
    )
    attachment_ids = fields.Many2many(
        'ir.attachment', 'premafirm_ai_reply_wizard_attachment_rel',
        string='Attachments',
        help='Attachments from the original inbound email — uncheck any you '
             'do not want to forward with the reply.',
    )
    prompt_version = fields.Char(
        string='AI Prompt Version', readonly=True,
        help='Business-profile prompt that produced this draft.',
    )
    subject = fields.Char(string='Subject', readonly=True)
    last_inbound_id = fields.Many2one(
        'mail.message', string='Replying To', readonly=True,
        help='The customer email this reply threads to.',
    )
    state = fields.Selection([('input', 'Input'), ('preview', 'Preview')], default='input')

    def action_generate_draft(self):
        self.ensure_one()
        lead = self.lead_id
        intent = (self.user_intent or '').strip()
        if not intent:
            return {'type': 'ir.actions.client', 'tag': 'reload'}

        # Build thread context
        thread_lines = []
        for msg in lead.message_ids.sorted('date', reverse=True)[:10]:
            if msg.message_type not in ('email', 'comment'):
                continue
            body = _strip_html(msg.body)
            if not body or len(body) < 10:
                continue
            direction = '→ YOU SENT' if (msg.author_id and msg.author_id.user_ids) else '← RECEIVED'
            ts = msg.date.strftime('%Y-%m-%d') if msg.date else '?'
            author = msg.author_id.name if msg.author_id else '?'
            thread_lines.append(f'[{direction} | {ts} | {author}]: {body[:400]}')

        partner = lead.partner_id
        first_name = (partner.name or '').split()[0] if partner else 'there'

        thread_text = '\n'.join(thread_lines) if thread_lines else '(no prior emails)'
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            system = profile.get_system_prompt()
        except Exception:
            system = PREMAFIRM_FALLBACK

        system += (
            f'\n\n=== YOUR TASK ===\n'
            f'Draft a professional reply email based on the user\'s intent below.\n'
            f'Rules: start with "Hi {first_name}," — keep it under 150 words unless more is needed — '
            f'no subject line — no signature — no preamble — freight industry tone.\n'
            f'Return ONLY the email body.'
        )

        user_msg = (
            f'My intent / what I want to say:\n{intent}\n\n'
            f'Email thread for context (newest first):\n{thread_text}'
        )

        # PHASE 9 — approval step context: thread target, Re: subject,
        # attachments from the original inbound, prompt version.
        svc = self.env['premafirm.mail.threading']
        last_inbound = svc._last_inbound_message(lead)
        last_msg = last_inbound[:1] if last_inbound else False
        orig_subject = (last_msg.subject if last_msg else '') or lead.name or ''
        if orig_subject and not orig_subject.lower().startswith('re:'):
            reply_subject = f'Re: {orig_subject}'
        else:
            reply_subject = orig_subject
        module_ver = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'premafirm_ai_engine')], limit=1).latest_version
        common = {
            'state': 'preview',
            'subject': reply_subject,
            'last_inbound_id': last_msg.id if last_msg else False,
            'attachment_ids': [(6, 0, last_msg.attachment_ids.ids)] if last_msg else [],
            'prompt_version': module_ver or False,
        }

        try:
            draft_text = _gpt(self.env, system, [{'role': 'user', 'content': user_msg}], max_tokens=500)
            draft_text = _strip_ai_meta(draft_text)
            html = '<br/>'.join(draft_text.replace('\r\n', '\n').split('\n'))
            common.update({'draft_body': html, 'final_body': html})
            self.write(common)
        except Exception as exc:
            self.write({**common, 'draft_body': f'<p>⚠ {exc}</p>'})

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_back_to_input(self):
        self.ensure_one()
        self.write({'state': 'input', 'draft_body': False})
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _build_ai_reply_mail(self):
        """PHASE 9 — one canonical outbound record for the approved reply.

        Creates (does NOT send) the single mail.mail for this approved AI
        reply: attached to the existing lead (model/res_id), threaded to
        the last inbound (References built from stored ids by the canonical
        service), To = the customer, CC/attachments from the approval step,
        canonical Reply-To (thread inbox alias), and AI provenance stamped
        (generated_by_ai, approved_by, approved_at, auto_sent=False).
        Never touches message_new — no new lead can be created.
        """
        self.ensure_one()
        lead = self.lead_id
        body = self.final_body or self.draft_body or ''

        # Signature: the user's own, falling back to the business profile.
        user_sig = (self.env.user.signature or '').strip()
        if not user_sig:
            try:
                profile = self.env['premafirm.business.profile'].sudo().get_profile()
                sig_text = (profile.email_signature or '').strip()
                if sig_text:
                    user_sig = '<br/>'.join(sig_text.replace('\r\n', '\n').split('\n'))
            except Exception:
                pass
        if user_sig:
            body = f'{body}<br/><br/>--<br/>{user_sig}'

        partner = lead.partner_id
        email_to = (partner.email or '').strip() if partner else ''
        if not email_to and self.last_inbound_id:
            email_to = (self.last_inbound_id.email_from or '').strip()
        if not email_to:
            raise ValueError(
                'No customer email address on the lead — add a contact before replying.'
            )

        svc = self.env['premafirm.mail.threading']
        values = svc.build_mail_values(
            lead,
            self.subject or lead.name or 'Re: your enquiry',
            body,
            email_to,
            email_cc=self.cc or None,
            auto_delete=False,
        )
        values.update({
            'model': 'crm.lead',
            'res_id': lead.id,
            # mail.mail-created messages carry no author by default — the
            # approving user is the sender.
            'author_id': self.env.user.partner_id.id,
        })
        mail = self.env['mail.mail'].create(values)
        if self.attachment_ids:
            mail.write({'attachment_ids': [(6, 0, self.attachment_ids.ids)]})
        mail._stamp_ai_provenance(prompt_version=self.prompt_version)
        # Outbound tracking (mail.mail create does not go through
        # message_post, so PHASE 8's _message_post_after_hook does not
        # fire): the reply advances last_outbound_at; only an initiated
        # touch (no parent inbound) advances last_outreach_at.
        lead.write({'last_outbound_at': fields.Datetime.now()})
        if not self.last_inbound_id:
            lead.write({'last_outreach_at': fields.Datetime.now()})
        # PHASE 14 — response discipline: complete Respond to Customer
        # activities, schedule the stage's next follow-up.
        lead._on_sales_response()
        return mail

    def action_approve_send(self):
        """Approve & Send — one canonical outbound email, stamped."""
        self.ensure_one()
        self._build_ai_reply_mail().send(raise_exception=False)
        return {'type': 'ir.actions.client', 'tag': 'reload'}


class CrmLeadAIReplyTrigger(models.Model):
    _inherit = 'crm.lead'

    def action_open_ai_reply_wizard(self):
        self.ensure_one()
        wizard = self.env['premafirm.crm.ai.reply.wizard'].create({'lead_id': self.id})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.crm.ai.reply.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }
