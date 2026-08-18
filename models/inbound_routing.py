"""PHASE 3 — robust reply routing (inbound side).

Core ``mail.thread.message_route`` already threads replies whose
References/In-Reply-To contain a stored openerp message-id
("direct reply to msg"). This module handles everything that FALLS
THROUGH to the crm.lead aliases:

* bounce / DSN mail          → silent drop (no lead, no engagement)
* out-of-office              → silent drop (no engagement)
* reply-like mail with stripped/mangled threading headers (the Cinelli
  case, PHASE 1 report §5)  → premafirm.inbound.queue for human review —
  NEVER a new lead, NEVER auto-linked by address alone
* internal-staff noise       → silent drop
* genuine new inquiry        → unchanged (creates the lead)

PHASES 4-5 deepen the classifier to the full 10 classes; the routing
boundary and the queue model are established here.

Implementation note: the classifier runs inside a ``message_route``
override (after super()) and REMOVES alias routes it absorbs. Core then
sees an empty route list and drops the message without posting —
matching how the dedupe and loop detectors already drop mail. The
empty-recordset ``message_new`` alternative is NOT viable: core posts
on the returned record (``thread.id``) and ``message_post`` calls
``ensure_one()``, so a falsy return would raise inside the fetchmail
cron. No core file changes.
"""
import logging
import re
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_INTERNAL_DOMAINS = ('premafirm.com', 'logistics.premafirm.com')

# Subject markers of a reply/forward whose threading headers were lost.
_REPLY_PREFIX_RE = re.compile(
    r'^\s*(?:re|fw|fwd|aw|sv|antwort|from)\s*[:›>]\s*', re.IGNORECASE
)
# Strip reply prefixes and quoting noise for subject similarity.
_SUBJECT_CLEAN_RE = re.compile(
    r'^\s*(?:re|fw|fwd|aw|sv|from)\s*[:›>]\s*|["\']|^\s*|\s*$', re.IGNORECASE
)

_BOUNCE_SUBJECTS = (
    'undeliverable', 'delivery failed', 'delivery status notification',
    'failure notice', 'invalid email address', 'message not delivered',
    'returned mail', 'mail delivery subsystem', 'undelivered mail',
    'non-delivery', 'delivery report', 'mailer-daemon',
)
_OOO_SUBJECTS = (
    'out of office', 'automatic reply', 'auto-reply', 'away from office',
    'out-of-office', 'vacation reply',
)


class PremafirmMailRouting(models.AbstractModel):
    _name = 'premafirm.mail.routing'
    _description = 'CRM inbound mail classifier (fall-through routing)'

    # ── Classification ─────────────────────────────────────────────

    @api.model
    def classify_fallthrough(self, msg_dict):
        """Classify mail that reached an alias without a thread match.

        Returns {'kind': ...} where kind is one of:
          'inquiry'          → genuine new inquiry (create the lead)
          'bounce'           → delivery failure/DSN (silent)
          'ooo'              → out-of-office / autoresponder (silent)
          'internal'         → internal staff noise (silent)
          'reply'            → reply/forward with lost threading → queue
        """
        subject = (msg_dict.get('subject') or '').strip()
        subject_l = subject.lower()
        email_from = msg_dict.get('email_from') or ''
        from_l = email_from.lower()

        if self._is_internal_sender(msg_dict):
            return {'kind': 'internal', 'subject': subject}

        if msg_dict.get('is_bounce') or self._looks_like_bounce(subject_l, from_l):
            return {'kind': 'bounce', 'subject': subject}

        if self._looks_like_ooo(subject_l):
            return {'kind': 'ooo', 'subject': subject}

        if self._looks_like_reply(subject, msg_dict.get('references'),
                                  msg_dict.get('in_reply_to')):
            return {
                'kind': 'reply',
                'subject': subject,
                'thread_candidate_id': self._find_thread_candidate(msg_dict),
            }

        return {'kind': 'inquiry', 'subject': subject}

    @api.model
    def _is_internal_sender(self, msg_dict):
        author_id = msg_dict.get('author_id')
        if not author_id:
            return False
        partner = self.env['res.partner'].browse(author_id).exists()
        return bool(partner and partner.user_ids)

    @api.model
    def _looks_like_bounce(self, subject_l, from_l):
        if any(m in subject_l for m in _BOUNCE_SUBJECTS):
            return True
        if 'mailer-daemon@' in from_l or 'postmaster@' in from_l:
            return True
        return False

    @api.model
    def _looks_like_ooo(self, subject_l):
        return any(o in subject_l for o in _OOO_SUBJECTS)

    @api.model
    def _looks_like_reply(self, subject, references, in_reply_to):
        if _REPLY_PREFIX_RE.match(subject or ''):
            return True
        # Belt and suspenders: a References/In-Reply-To that somehow
        # missed core's match still means "reply to a known thread".
        if references or in_reply_to:
            return True
        return False

    @api.model
    def _find_thread_candidate(self, msg_dict):
        """Best-guess thread for an unmatched reply, for the human
        reviewer — NEVER used to auto-link.

        Matches the cleaned subject against outbound subjects of the
        sender's leads in the last 90 days. One exact match → candidate;
        none or several → False (ambiguity goes to the queue unbound)."""
        author_id = msg_dict.get('author_id')
        if not author_id:
            return False
        target = _SUBJECT_CLEAN_RE.sub('', msg_dict.get('subject') or '').strip().lower()
        if len(target) < 5:
            return False

        cutoff = fields.Datetime.now() - timedelta(days=90)
        leads = self.env['crm.lead'].sudo().search([
            ('partner_id', '=', author_id),
            ('date_last_stage_update', '>=', cutoff),
        ])
        matches = []
        for lead in leads:
            outbound = lead.message_ids.filtered(
                lambda m: m.message_type in ('comment', 'email_outgoing')
                and m.author_id and m.author_id.user_ids
            )
            for msg in outbound:
                stored = _SUBJECT_CLEAN_RE.sub('', msg.subject or '').strip().lower()
                if stored and (target.startswith(stored) or stored.startswith(target)):
                    matches.append(lead.id)
                    break
            if len(matches) > 1:
                return False
        return matches[0] if len(matches) == 1 else False

    # ── Outcomes ───────────────────────────────────────────────────

    @api.model
    def handle_silent(self, kind, msg_dict):
        """Bounce / OOO / internal noise: drop with an audit log line.
        (PHASE 4 correlates bounces back to their mail.mail rows.)"""
        _logger.info(
            'premafirm.mail.routing: %s mail dropped (no lead, no engagement) '
            'from %s to %s, subject "%s", Message-Id %s',
            kind, msg_dict.get('email_from'), msg_dict.get('to'),
            msg_dict.get('subject'), msg_dict.get('message_id'),
        )

    @api.model
    def queue_inbound(self, verdict, msg_dict):
        """Unmatched reply → Inbound Review Queue record."""
        author_id = msg_dict.get('author_id')
        queue = self.env['premafirm.inbound.queue'].sudo().create({
            'name': (verdict.get('subject') or 'No Subject')[:250],
            'email_from': (msg_dict.get('email_from') or '')[:250],
            'partner_id': author_id or False,
            'message_id': msg_dict.get('message_id') or False,
            'mailbox': (msg_dict.get('to') or '')[:250],
            'classification': 'reply',
            'thread_candidate_id': verdict.get('thread_candidate_id') or False,
            'body': msg_dict.get('body') or '',
        })
        _logger.info(
            'premafirm.mail.routing: unmatched reply queued (id %s, thread candidate %s) '
            'from %s, subject "%s"',
            queue.id, queue.thread_candidate_id.id or 0,
            msg_dict.get('email_from'), msg_dict.get('subject'),
        )
        return queue


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    @api.model
    def message_route(self, message, message_dict, model=None, thread_id=None,
                      custom_values=None):
        routes = super().message_route(message, message_dict, model, thread_id,
                                       custom_values)
        # Only alias fall-through routes on crm.lead are candidates.
        # Reference-matched routes (threaded replies) are left untouched.
        absorb = False
        for _model, tid, _cv, _uid, alias in routes or ():
            if _model == 'crm.lead' and not tid and alias:
                verdict = self.env['premafirm.mail.routing'].classify_fallthrough(message_dict)
                kind = verdict['kind']
                if kind == 'inquiry':
                    continue  # genuine new inquiry → keep the route
                if kind == 'reply':
                    self.env['premafirm.mail.routing'].queue_inbound(verdict, message_dict)
                else:
                    self.env['premafirm.mail.routing'].handle_silent(kind, message_dict)
                absorb = True
        if absorb:
            routes = [
                (m, t, cv, u, al) for m, t, cv, u, al in routes
                if not (m == 'crm.lead' and not t and al)
            ]
        return routes


class PremafirmInboundQueue(models.Model):
    _name = 'premafirm.inbound.queue'
    _description = 'Inbound Review Queue — unmatched customer email'
    _order = 'id desc'

    name = fields.Char('Subject', required=True, index=True)
    email_from = fields.Char('From', required=True)
    partner_id = fields.Many2one('res.partner', 'Sender', ondelete='set null',
                                 index=True)
    message_id = fields.Char('Message-Id', index=True)
    mailbox = fields.Char('Addressed To', help='The alias/mailbox the mail hit.')
    classification = fields.Selection([
        ('reply', 'Unmatched Reply'),
        ('bounce', 'Bounce'),
        ('ooo', 'Out of Office'),
        ('internal', 'Internal Noise'),
        ('other', 'Other'),
    ], default='other', required=True, index=True)
    thread_candidate_id = fields.Many2one('crm.lead', 'Likely Thread',
                                          ondelete='set null', index=True,
                                          help='Best guess only — always verify '
                                               'before threading.')
    body = fields.Html('Body')
    state = fields.Selection([
        ('new', 'New'),
        ('reviewed', 'Reviewed'),
        ('threaded', 'Threaded to Lead'),
        ('ignored', 'Ignored'),
        ('lead_created', 'Lead Created'),
    ], default='new', required=True, index=True)
    review_note = fields.Text('Review Note')

    def action_mark_reviewed(self):
        self.write({'state': 'reviewed'})
        return True

    def action_mark_ignored(self):
        self.write({'state': 'ignored'})
        return True

    def action_thread_to_lead(self):
        """Human-confirmed attach: post the stored mail onto the lead's
        chatter as a comment (no message_id — keeps dedupe clean) and
        close the queue record."""
        for rec in self:
            if not rec.thread_candidate_id:
                continue
            lead = rec.thread_candidate_id
            lead.message_post(
                body=rec.body or '<p>(no body captured)</p>',
                subject='Imported from Inbound Review Queue: %s' % (rec.name or ''),
                subtype_xmlid='mail.mt_comment',
            )
            rec.write({'state': 'threaded', 'review_note': 'Threaded to lead %s' % lead.id})
        return True

    def action_create_lead(self):
        """Human decision: this unmatched mail is a genuine new inquiry —
        create the lead from the captured mail."""
        Lead = self.env['crm.lead'].sudo()
        for rec in self:
            lead = Lead.create({
                'name': rec.name or 'No Subject',
                'email_from': rec.email_from,
                'partner_id': rec.partner_id.id or False,
            })
            lead.message_post(
                body=rec.body or '<p>(no body captured)</p>',
                subject='Imported from Inbound Review Queue: %s' % (rec.name or ''),
                subtype_xmlid='mail.mt_comment',
            )
            rec.write({'state': 'lead_created', 'review_note': 'Lead %s created' % lead.id})
        return True
