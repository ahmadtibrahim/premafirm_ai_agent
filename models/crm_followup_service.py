"""PHASE 15 — One consolidated CRM Follow-Up Service.

Replaces the six legacy crons (ir_cron_crm_followup, ir_cron_cold_reactivation,
ir_cron_replied_warning, ir_cron_replied_stale, ir_cron_outreach_stale and
ir_cron_contact_rotation) with ONE daily cron and ONE entry point —
``run_followup_service_cron``. The legacy crons are deactivated by the
18.0.6.15.0 migration (never deleted — history), and their AI drafting
helpers are reused here.

The service is a DRAFT GENERATOR by default. It never emails a customer
unless the operator opts in explicitly via ``crm.followup.send_mode``:

  draft (default): AI follow-up drafts are posted as thread NOTES for
      human review — nothing leaves the CRM.
  approval:        the draft is posted AND a "Review & send" To-Do is
      scheduled for the lead owner.
  auto:            the draft is sent through the canonical outbound
      service (premafirm.mail.threading — same path as PHASE 9's
      approve-send) — but ONLY to leads that have already been contacted
      (never a first touch) and NEVER to inboxes that bounced, sent an
      out-of-office, or flagged spam (bounces/OOO never count as
      engagement and are never re-emailed).

Safety (non-negotiable):
  * the service NEVER moves pipeline stages — the stale/no-reply flags
    are review activities for a human, never automated stage moves
  * no duplicate drafts — deduped on thread marker + sent stamp
  * drafts are posted as notes (mail.mt_note) — nothing customer-facing
    is sent in draft/approval modes
  * automation 63 (website callback) retargeted off the archived
    "Call Back" stage id by the migration (name-resolved OUTREACH SENT)

Timings are ir.config_parameter — never hardcoded:
  crm.followup.outreach_followup_bizdays = 3   follow-up #1 after N business days of silence
  crm.followup.final_followup_bizdays   = 5   follow-up #2 after follow-up #1
  crm.followup.cold_days                = 60  cold-lead reactivation cutoff
  crm.followup.engaged_reply_days       = 3   reply-without-response warning
  crm.followup.stale_reply_days         = 6   stale-reply manual review
  crm.followup.no_reply_review_days     = 7   outreach no-reply manual review
  crm.followup.send_mode                = draft | approval | auto
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models

from .crm_followup import (SEASONAL, _biz_days_since, _get_thread,
                           _gpt_draft)

_logger = logging.getLogger(__name__)

_TODO_XMLID = 'mail.mail_activity_data_todo'
_RESPOND_TYPE_XMLID = 'premafirm_ai_engine.premafirm_activity_respond_customer'

# param key → (default, help). All timings configurable, never hardcoded.
_FOLLOWUP_PARAMS = {
    'crm.followup.outreach_followup_bizdays': (3, 'follow-up #1 biz days'),
    'crm.followup.final_followup_bizdays': (5, 'follow-up #2 after #1'),
    'crm.followup.cold_days': (60, 'cold reactivation cutoff'),
    'crm.followup.engaged_reply_days': (3, 'reply-without-response warning'),
    'crm.followup.stale_reply_days': (6, 'stale-reply manual review'),
    'crm.followup.no_reply_review_days': (7, 'outreach no-reply review'),
}
_SEND_MODES = ('draft', 'approval', 'auto')

# Inbound classifications that must never be re-emailed (bounces/OOO/
# spam are not engagement) and never counted as engagement.
_SUPPRESSED_INBOUND = ('bounce', 'out_of_office', 'spam')

_DRAFT_MARKERS = {
    1: 'AI Follow-up Draft #1',
    2: 'AI Follow-up Draft #2',
}


class CrmFollowupService(models.Model):
    _inherit = 'crm.lead'

    # ── config ─────────────────────────────────────────────────────────

    @api.model
    def _followup_param(self, key):
        default, _help = _FOLLOWUP_PARAMS[key]
        raw = self.env['ir.config_parameter'].sudo().get_param(
            key, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            _logger.warning('PHASE 15: param %s=%r not an int — using %s',
                            key, raw, default)
            return default

    @api.model
    def _followup_send_mode(self):
        mode = self.env['ir.config_parameter'].sudo().get_param(
            'crm.followup.send_mode', 'draft')
        if mode not in _SEND_MODES:
            _logger.warning(
                'PHASE 15: crm.followup.send_mode=%r invalid — using draft',
                mode)
            return 'draft'
        return mode

    @api.model
    def _followup_stage(self, norm_name):
        """One target stage by xmlid (deterministic — see PHASE 13)."""
        return self._premafirm_target_stages()[norm_name]

    # ── dedupe ─────────────────────────────────────────────────────────

    @api.model
    def _already_drafted(self, marker, since=None):
        """Marker already on the thread (any prior run, any mode) — or the
        legacy sent stamp is set. Never draft the same follow-up twice.
        Called record-bound (``lead._already_drafted(marker)``)."""
        self.ensure_one()
        dom = [('res_id', '=', self.id), ('model', '=', 'crm.lead'),
               ('body', 'ilike', marker)]
        if since:
            dom.append(('date', '>=', since))
        if self.env['mail.message'].sudo().search_count(dom):
            return True
        if marker == _DRAFT_MARKERS[1] and self.x_followup_1_sent_at:
            return True
        if marker == _DRAFT_MARKERS[2] and self.x_followup_2_sent_at:
            return True
        return False

    # ── To-Do reviews (activities, never stage moves) ──────────────────

    def _schedule_review(self, summary, note, deadline=None):
        """Human-review To-Do, deduped on open same-summary. Odoo 18:
        activity_schedule takes the type XMLID string, not the id."""
        self.ensure_one()
        todo = self.env.ref(_TODO_XMLID, raise_if_not_found=False)
        if not todo:
            todo = self.env['mail.activity.type'].sudo().search(
                [('name', '=', 'To-Do')], limit=1)
        if not todo:
            return False
        existing = self.env['mail.activity'].search([
            ('res_model', '=', 'crm.lead'), ('res_id', '=', self.id),
            ('summary', '=', summary), ('active', '=', True)], limit=1)
        if existing:
            return False
        self.activity_schedule(
            _TODO_XMLID, summary=summary, note=note,
            date_deadline=deadline or fields.Date.today(),
            user_id=(self.user_id.id or self.env.uid))
        _logger.info('PHASE 15: review activity on lead %s: %s',
                     self.id, summary)
        return True

    # ── draft dispatch (draft / approval / auto) ───────────────────────

    def _dispatch_draft(self, marker, draft_text, context_html):
        """Post the AI draft per send mode; stamp the sent marker so no
        mode ever duplicates a follow-up."""
        self.ensure_one()
        mode = self._followup_send_mode()
        body = Markup(
            f'<b>📬 {marker}</b><br/>{Markup.escape(context_html)}<br/>')
        if draft_text:
            body += Markup(
                f'<pre style="white-space:pre-wrap;font-family:inherit">'
                f'{Markup.escape(draft_text)}</pre>')
        else:
            body += Markup(
                '<i>Could not generate a draft (API key issue) — '
                'follow up manually.</i>')

        if mode == 'auto':
            sent = self._send_auto(body)
            if sent:
                self._stamp_followup(marker)
                return 'sent'
            # send refused or failed → fall through to a thread draft so
            # the follow-up is never silently lost
            self.message_post(body=body, subtype_xmlid='mail.mt_note')
            self._stamp_followup(marker)
            return 'drafted-refused'

        self.message_post(body=body, subtype_xmlid='mail.mt_note')
        if mode == 'approval':
            self._schedule_review(
                summary='Review & send follow-up',
                note='AI-generated follow-up draft posted on this thread '
                     '— review and send when ready.')
        self._stamp_followup(marker)
        return 'drafted'

    def _stamp_followup(self, marker):
        if marker == _DRAFT_MARKERS[1]:
            self.sudo().write({'x_followup_1_sent_at': fields.Datetime.now()})
        elif marker == _DRAFT_MARKERS[2]:
            self.sudo().write({'x_followup_2_sent_at': fields.Datetime.now()})

    def _send_auto(self, body):
        """Canonical outbound send (premafirm.mail.threading) with the
        suppression guards: prior outreach exists (search guarantees it),
        a real recipient, and no bounce/OOO/spam inbound."""
        self.ensure_one()
        if self.last_inbound_classification in _SUPPRESSED_INBOUND:
            _logger.info(
                'PHASE 15: auto-send skipped lead %s — suppressed inbound '
                '(%s)', self.id, self.last_inbound_classification)
            return False
        partner = self.partner_id
        email_to = (partner.email or '').strip() if partner else ''
        if not email_to and self.email_from:
            email_to = (self.email_from or '').strip()
        if not email_to:
            _logger.info('PHASE 15: auto-send skipped lead %s — no email',
                         self.id)
            return False
        try:
            svc = self.env['premafirm.mail.threading']
            company = partner.parent_id if partner else False
            subject = 'Following up — %s' % (
                company.name if company else (partner.name if partner
                                              else 'our conversation'))
            values = svc.build_mail_values(
                self, subject, body, email_to, auto_delete=False)
            values.update({
                'model': 'crm.lead', 'res_id': self.id,
                'author_id': (self.user_id.partner_id
                              or self.env.user.partner_id).id,
            })
            mail = self.env['mail.mail'].create(values)
            mail.send(raise_exception=False)
        except Exception as exc:
            _logger.warning('PHASE 15: auto-send failed lead %s: %s',
                            self.id, exc)
            return False
        # outbound stamps + PHASE 14 response discipline (initiated touch
        # advances last_outreach_at as well as last_outbound_at)
        self.write({'last_outbound_at': fields.Datetime.now(),
                    'last_outreach_at': fields.Datetime.now()})
        self._on_sales_response()
        _logger.info('PHASE 15: auto-sent follow-up on lead %s', self.id)
        return True

    # ── the one entry point ────────────────────────────────────────────

    @api.model
    def run_followup_service_cron(self):
        """The consolidated daily follow-up service (replaces the six
        legacy crons). Every leg is idempotent and never moves stages."""
        summary = {'followups': 0, 'reactivations': 0,
                   'reply_warnings': 0, 'stale_reviews': 0,
                   'outreach_reviews': 0, 'skipped': []}
        try:
            summary['followups'] = self._generate_followup_drafts()
        except Exception as exc:
            _logger.error('PHASE 15: followup leg failed: %s', exc)
        try:
            summary['reactivations'] = self._generate_reactivation_drafts()
        except Exception as exc:
            _logger.error('PHASE 15: reactivation leg failed: %s', exc)
        try:
            summary['reply_warnings'] = self._flag_reply_warnings()
        except Exception as exc:
            _logger.error('PHASE 15: reply-warning leg failed: %s', exc)
        try:
            summary['stale_reviews'] = self._flag_stale_reviews()
        except Exception as exc:
            _logger.error('PHASE 15: stale-review leg failed: %s', exc)
        try:
            summary['outreach_reviews'] = self._flag_outreach_reviews()
        except Exception as exc:
            _logger.error('PHASE 15: outreach-review leg failed: %s', exc)
        _logger.info('PHASE 15: follow-up service run — %s', summary)
        return summary

    # ── leg 1: AI follow-up drafts (FU1 / FU2) ─────────────────────────

    @api.model
    def _generate_followup_drafts(self):
        """FU1 after outreach_followup_bizdays of silence; FU2 after
        final_followup_bizdays more. Only leads we have already reached
        out to and who have never replied."""
        targets = self._premafirm_target_stages()
        leads = self.sudo().search([
            ('active', '=', True),
            ('date_closed', '=', False),
            ('reply_received', '=', False),
            ('last_outreach_at', '!=', False),
            ('stage_id', 'in',
             [targets['new / uncontacted'].id,
              targets['outreach sent'].id]),
        ])
        fu1_days = self._followup_param(
            'crm.followup.outreach_followup_bizdays')
        fu2_days = self._followup_param('crm.followup.final_followup_bizdays')
        done = 0
        for lead in leads:
            try:
                if not lead._already_drafted(_DRAFT_MARKERS[1]):
                    biz = _biz_days_since(lead.last_outreach_at)
                    if biz >= fu1_days:
                        self._post_followup_draft(lead, 1, biz)
                        done += 1
                        continue
                if (lead._already_drafted(_DRAFT_MARKERS[1])
                        and not lead._already_drafted(_DRAFT_MARKERS[2])):
                    fu1_at = lead.x_followup_1_sent_at
                    if fu1_at and _biz_days_since(fu1_at) >= fu2_days:
                        self._post_followup_draft(lead, 2, fu2_days)
                        done += 1
            except Exception as exc:
                _logger.warning('PHASE 15: follow-up draft failed lead %s: %s',
                                lead.id, exc)
        return done

    def _post_followup_draft(self, lead, num, biz):
        """Generate (AI) + dispatch the follow-up for a lead."""
        lead = lead.sudo()
        contact = lead.partner_id
        contact_name = (contact.name if contact
                        else lead.partner_name or 'the contact')
        company = contact.parent_id if contact and contact.parent_id else None
        company_name = company.name if company else contact_name
        title = (contact.function if contact and contact.function
                 else 'unknown title')
        thread = _get_thread(lead)
        system = (
            'You are Ahmad Ibrahim\'s AI freight sales assistant at '
            'PremaFirm Inc., a Canadian trucking carrier. Write concise '
            'freight sales follow-up emails. Professional, warm, gets to '
            'the point. Under 100 words unless asked otherwise. Do NOT '
            'include a signature — Odoo adds it automatically.'
        )
        if num == 1:
            prompt = (
                f'Write a follow-up email to {contact_name} ({title}) at '
                f'{company_name}. {biz} business days since last contact. '
                f'Use a value-add angle — offer something useful (lane '
                f'capacity, seasonal insight, availability window). Do not '
                f'just say "checking in". Previous thread:\n{thread}'
            )
        else:
            prompt = (
                f'Write a final follow-up email to {contact_name} at '
                f'{company_name}. {biz} business days since last contact. '
                f'Still no reply. Mild urgency angle — mention limited '
                f'capacity or upcoming peak season. Clear call to action. '
                f'Under 80 words. Previous thread:\n{thread}'
            )
        draft = _gpt_draft(self.env, system, prompt)
        context_html = (
            f'Follow-up #{num} — {biz} business days since last outreach '
            f'to {contact_name} at {company_name}. '
            f'<i>Review and send manually when ready.</i>'
        )
        lead._dispatch_draft(_DRAFT_MARKERS[num], draft, context_html)

    # ── leg 2: cold reactivation ───────────────────────────────────────

    @api.model
    def _generate_reactivation_drafts(self):
        """Leads silent 60+ days get one reactivation draft (never
        duplicated within a 30-day window).  B-10 guard: folded stages
        (LOST, PAUSED / ON HOLD — any hold stage) and won records are
        NEVER reactivated — whatever ``crm.followup.send_mode`` says —
        because they are no longer prospect follow-up; the lead stays
        where the salesperson parked it."""
        cutoff = fields.Datetime.now() - timedelta(
            days=self._followup_param('crm.followup.cold_days'))
        seasonal_hint = SEASONAL.get(
            fields.Date.context_today(self), '')
        leads = self.sudo().search([
            ('active', '=', True),
            ('date_closed', '=', False),
            ('reply_received', '=', False),
            ('last_outreach_at', '!=', False),
            ('last_outreach_at', '<', cutoff),
            # B-10 stage guard — independent of the follow-up send mode:
            # no reactivation drafts/emails for LOST, PAUSED / ON HOLD or
            # any other folded/hold stage, and none for won records.
            ('stage_id.fold', '=', False),
            ('stage_id.is_won', '=', False),
        ])
        recent = fields.Datetime.now() - timedelta(days=30)
        done = 0
        for lead in leads:
            try:
                if lead._already_drafted('Reactivation', since=recent):
                    continue
                self._post_reactivation_draft(lead, seasonal_hint)
                done += 1
            except Exception as exc:
                _logger.warning(
                    'PHASE 15: reactivation failed lead %s: %s',
                    lead.id, exc)
        return done

    def _post_reactivation_draft(self, lead, seasonal_hint=''):
        lead = lead.sudo()
        contact = lead.partner_id
        contact_name = (contact.name if contact
                        else lead.partner_name or 'the contact')
        company = contact.parent_id if contact and contact.parent_id else None
        company_name = company.name if company else contact_name
        days = _biz_days_since(lead.last_outreach_at)
        thread = _get_thread(lead, limit=4)
        seasonal = (f'\nSEASONAL CONTEXT: {seasonal_hint}'
                    if seasonal_hint else '')
        prompt = (
            f'Write a reactivation email to {contact_name} at '
            f'{company_name}. {days} business days since last contact — '
            f'this account has gone cold. Reference the previous '
            f'conversation briefly, offer fresh value. Warm but '
            f'professional. Under 90 words. Do NOT include a '
            f'signature.{seasonal}\n\nPrevious thread:\n{thread}'
        )
        draft = _gpt_draft(
            self.env,
            'You are Ahmad Ibrahim\'s AI freight sales assistant. Write '
            'reactivation emails that feel personal and relevant, not '
            'generic.',
            prompt,
        )
        context_html = (
            f'Cold Lead Reactivation — {company_name}. '
            f'Last contact: {days} business days ago | '
            f'Contact: {contact_name}'
            + (f'<br/>📅 {seasonal_hint}' if seasonal_hint else '')
        )
        lead._dispatch_draft('Reactivation', draft, context_html)

    # ── leg 3: reply-without-response warning ──────────────────────────

    @api.model
    def _flag_reply_warnings(self):
        """ENGAGED / REPLIED leads whose meaningful reply is N business
        days old with no outbound after it get a "Write follow-up reply"
        warning. The PHASE 14 Respond to Customer activity is the primary
        signal — this is a safety net and is skipped while one is open."""
        engaged = self._followup_stage('engaged / replied')
        days = self._followup_param('crm.followup.engaged_reply_days')
        cutoff = fields.Datetime.now() - timedelta(days=days)
        respond_id = self.env.ref(_RESPOND_TYPE_XMLID).id
        leads = self.sudo().search([
            ('active', '=', True),
            ('date_closed', '=', False),
            ('stage_id', '=', engaged.id),
            ('last_meaningful_reply_at', '!=', False),
            ('last_meaningful_reply_at', '<=', cutoff),
        ])
        done = 0
        for lead in leads:
            try:
                if (lead.last_outbound_at
                        and lead.last_outbound_at > lead.last_meaningful_reply_at):
                    continue  # we already responded
                if lead.activity_ids.filtered(
                        lambda a: a.activity_type_id.id == respond_id):
                    continue  # PHASE 14 discipline is on it
                if lead._schedule_review(
                        summary='Write follow-up reply',
                        note=f'Customer replied {days}+ days ago with no '
                             f'response sent — write a follow-up now.'):
                    done += 1
            except Exception as exc:
                _logger.warning('PHASE 15: reply warning failed lead %s: %s',
                                lead.id, exc)
        return done

    # ── leg 4: stale-reply manual review ───────────────────────────────

    @api.model
    def _flag_stale_reviews(self):
        """ENGAGED / REPLIED leads whose reply is stale_reply_days old
        with no outbound after it get a manual-review flag (never a
        stage move)."""
        engaged = self._followup_stage('engaged / replied')
        days = self._followup_param('crm.followup.stale_reply_days')
        cutoff = fields.Datetime.now() - timedelta(days=days)
        leads = self.sudo().search([
            ('active', '=', True),
            ('date_closed', '=', False),
            ('stage_id', '=', engaged.id),
            ('last_meaningful_reply_at', '!=', False),
            ('last_meaningful_reply_at', '<=', cutoff),
        ])
        done = 0
        for lead in leads:
            try:
                if (lead.last_outbound_at
                        and lead.last_outbound_at > lead.last_meaningful_reply_at):
                    continue
                if lead._schedule_review(
                        summary='Review replied lead for manual Data Collection move',
                        note=f'Customer replied {days}+ days ago with no '
                             f'follow-up sent. Review manually and move to '
                             f'Data Collection only if needed.'):
                    done += 1
            except Exception as exc:
                _logger.warning('PHASE 15: stale review failed lead %s: %s',
                                lead.id, exc)
        return done

    # ── leg 5: outreach no-reply manual review ─────────────────────────

    @api.model
    def _flag_outreach_reviews(self):
        """OUTREACH SENT / NEW / UNCONTACTED leads silent N days with no
        reply get a manual-review flag (never a stage move)."""
        targets = self._premafirm_target_stages()
        days = self._followup_param('crm.followup.no_reply_review_days')
        cutoff = fields.Datetime.now() - timedelta(days=days)
        leads = self.sudo().search([
            ('active', '=', True),
            ('date_closed', '=', False),
            ('reply_received', '=', False),
            ('last_outreach_at', '!=', False),
            ('last_outreach_at', '<=', cutoff),
            ('stage_id', 'in',
             [targets['new / uncontacted'].id,
              targets['outreach sent'].id]),
        ])
        done = 0
        for lead in leads:
            try:
                if lead._schedule_review(
                        summary='Review for manual Data Collection move',
                        note=f'No reply after {days}+ days of outreach. '
                             f'Review this lead manually and move it to '
                             f'Data Collection only if needed.'):
                    done += 1
            except Exception as exc:
                _logger.warning('PHASE 15: outreach review failed lead %s: %s',
                                lead.id, exc)
        return done
