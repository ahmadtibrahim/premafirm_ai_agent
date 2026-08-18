"""PHASES 3-5 — robust inbound routing: classify EVERY inbound email
BEFORE CRM lead creation.

Core ``mail.thread.message_route`` threads replies whose
References/In-Reply-To contain a stored openerp message-id ("direct reply
to msg") and drops natively-detected bounces (``is_bounce`` →
``_routing_handle_bounce``, audited: detection covers bounce-alias To,
mailer-daemon From, multipart/report content-type only — and marks
``mail.notification`` rows + bumps blacklist counters; never creates
leads). This module handles EVERYTHING that falls through to the
crm.lead aliases, with the full 10-class classification of the spec:

  normal_reply       reply matched to its thread by core (keep route)
  new_inquiry        genuine new inquiry (keep route → lead created)
  unmatched_reply    reply-lookalike with lost/mangled threading → queue
  bounce             generic bounce signature (silent, evidence, audit)
  delivery_failure   DSN with Status/Action/Diagnostic-Code (silent,
                     evidence, permanent → bounce counter/suppression)
  out_of_office      vacation auto-reply (queue, pre-bound thread, never
                     engagement, optional return date)
  auto_reply         generic auto-replier (same handling as OOO)
  mailing_list       digest/newsletter (silent)
  system_message     internal staff noise / no-reply system mail (silent)
  spam_or_noise      junk (silent)

Hard rules from the spec:
  * bounces/OOO never count as engagement, never create leads
  * unmatched replies NEVER become new leads — they go to the
    premafirm.inbound.queue for human review (Attach to Opportunity /
    Create New Lead / Ignore / Mark Bounce / Mark OOO)
  * genuine new inquiries to accounts@/quotes@/sales@ still create leads
  * never link by email address alone (thread_candidate_id is a
    best-guess for the human reviewer, never auto-applied)

Implementation note: the classifier runs inside a ``message_route``
override (after super()) and REMOVES the alias routes it absorbs. Core
then sees an empty route list and drops the message without posting —
matching how the dedupe and loop detectors already drop mail. The
empty-recordset ``message_new`` alternative is NOT viable: core posts
on the returned record (``thread.id``) and ``message_post`` calls
``ensure_one()``, so a falsy return would raise inside the fetchmail
cron. No core file changes.
"""
import logging
import re
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.tools import email_normalize

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
    'non-delivery', 'delivery report', 'mailer-daemon', 'undeliverable mail',
)
_OOO_SUBJECTS = (
    'out of office', 'automatic reply', 'auto-reply', 'away from office',
    'out-of-office', 'vacation reply', 'out of the office',
)
_OOO_BODY_MARKERS = (
    'vacation', 'out of office', 'out of the office', 'on holiday',
    'on vacation', 'away until', 'away from', 'back on', 'returning on',
    'will be back', 'annual leave',
)
_AUTO_SUBMITTED_VALUES = ('auto-replied', 'auto-generated')
_AUTO_REPLY_SUPPRESS = ('all', 'oof', 'autoreply')

_MAILING_LIST_HEADERS = ('list-id', 'list-unsubscribe', 'list-subscribe',
                         'list-help', 'x-mailing-list', 'x-list-name',
                         'list-post')
_DIGEST_SENDERS = (
    'linkedin.com', 'facebook.com', 'twitter.com', 'x.com', 'zoom.us',
    'teams.microsoft.com', 'notifications@google.com', 'google.com',
    'meetup.com', 'eventbrite.com', 'salesforce.com',
)
_DIGEST_SUBJECT_MARKERS = ('weekly digest', 'digest', 'newsletter',
                           'unsubscribe', 'your .* summary')

_SYSTEM_LOCALPARTS = (
    'no-reply', 'noreply', 'donotreply', 'do-not-reply', 'notification',
    'notifications', 'system', 'alert', 'alerts', 'robot', 'automation',
    'service', 'support', 'it-support', 'admin', 'administrator',
)
_SYSTEM_SUBJECT_MARKERS = (
    'notification', 'alert', 'verification', 'password', 'security',
    'otp', '2fa', 'sign-in', 'sign in', 'account activity', 'invoice paid',
    'payment received', 'receipt for your payment',
)

_SPAM_PATTERNS = (
    r'viagra', r'casino', r'\bwire transfer\b', r'\binheritance\b',
    r'\blottery\b', r'\bcryptocurrency\b', r'bitcoin',
    r'\burgent action required\b', r'\btransfer of funds\b',
    r'\$\$\$', r'\b(?:free|cheap)\s+(?:viagra|meds|pills)\b',
    r'\bmoney\s+laundering\b', r'\bprince\b.*\bmoney\b',
)

# "I will be back on Aug 20" / "returning Monday" / "away until 24/08"
_RETURN_DATE_RES = (
    re.compile(r'\bback\s+on\s+(\w+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)', re.I),
    re.compile(r'\breturning\s+(?:on\s+)?(\w+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)', re.I),
    re.compile(r'\b(?:away|out)\s+(?:of\s+(?:the\s+)?office\s+)?(?:until|till)\s+(\w+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)', re.I),
    re.compile(r'\b(?:on\s+)?(?:vacation|holiday|leave)\s+until\s+(\w+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)', re.I),
    re.compile(r'\bwill\s+be\s+back\s+(?:on\s+)?(\w+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)', re.I),
)
_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
     'nov', 'dec'], start=1)}


class PremafirmMailRouting(models.AbstractModel):
    _name = 'premafirm.mail.routing'
    _description = 'CRM inbound mail classifier (10 classes, before lead creation)'

    # ── Entry point ─────────────────────────────────────────────────

    @api.model
    def classify(self, msg_dict, raw_message=None, matched=False):
        """Classify an inbound email that reached an alias route.

        :param msg_dict: parsed message dict (message_parse output)
        :param raw_message: raw RFC822 email.message.Message when the
            caller has it (fetchmail path) — used for headers/parts that
            message_parse does not expose (Return-Path, DSN parts)
        :param matched: True when core already matched the mail to a
            thread (route has thread_id) — the reply-lookalike class is
            then ``normal_reply`` instead of ``unmatched_reply``
        :returns: verdict dict with 'kind' (one of the 10 classes) and
            optional detail/dsn/ooo_return_date/thread_candidate_id
        """
        subject = (msg_dict.get('subject') or '').strip()
        subject_l = subject.lower()
        email_from = msg_dict.get('email_from') or ''
        from_l = email_from.lower()

        # 1. internal staff noise — never engages, never queues
        if self._is_internal_sender(msg_dict):
            return {'kind': 'system_message', 'subject': subject,
                    'detail': 'internal staff sender'}

        # 2. bounces / delivery failures — DSN evidence first (raw
        #    headers/parts core's is_bounce detection does not cover),
        #    subject/from signatures secondary
        dsn = self._dsn_evidence(msg_dict, raw_message)
        if (dsn or msg_dict.get('is_bounce')
                or self._looks_like_bounce(subject_l, from_l)):
            if dsn and (dsn.get('permanent') or dsn.get('transient')
                        or dsn.get('action_failed') or dsn.get('diagnostic')):
                kind = 'delivery_failure'
            else:
                kind = 'bounce'
            return {'kind': kind, 'subject': subject, 'dsn': dsn or {}}

        # 3. out-of-office / auto-reply — headers first, subject secondary
        auto = self._auto_reply_evidence(msg_dict, raw_message)
        if auto or self._looks_like_ooo(subject_l):
            is_ooo = auto in ('auto-replied',) or self._ooo_markers(subject_l)
            ooo_date = self._extract_return_date(
                subject, msg_dict.get('body') or '') if is_ooo else False
            return {'kind': 'out_of_office' if is_ooo else 'auto_reply',
                    'subject': subject, 'ooo_return_date': ooo_date,
                    'auto_submitted': auto}

        # 4. mailing lists / digests — List-* headers or known senders
        if self._is_mailing_list(msg_dict, raw_message, from_l, subject_l):
            return {'kind': 'mailing_list', 'subject': subject}

        # 5. reply-lookalikes — threading headers, Re:/quoted body,
        #    or a sender we contacted recently
        if self._looks_like_reply(subject, msg_dict.get('references'),
                                  msg_dict.get('in_reply_to'),
                                  msg_dict.get('body')) or self._recently_contacted(msg_dict):
            verdict = {'subject': subject}
            if matched:
                verdict['kind'] = 'normal_reply'
            else:
                verdict['kind'] = 'unmatched_reply'
                verdict['thread_candidate_id'] = self._find_thread_candidate(msg_dict)
            return verdict

        # 6. system messages — no-reply senders + system subjects
        if self._is_system_message(from_l, subject_l):
            return {'kind': 'system_message', 'subject': subject,
                    'detail': 'system sender/subject'}

        # 7. junk — conservative patterns only; anything ambiguous stays
        if self._is_spam_noise(subject, from_l):
            return {'kind': 'spam_or_noise', 'subject': subject}

        # 8. genuine new inquiry
        return {'kind': 'new_inquiry', 'subject': subject}

    # ── Raw-message analysis (headers/parts message_parse drops) ────

    @api.model
    def _raw_header(self, raw_message, header_name):
        if raw_message is None or not hasattr(raw_message, 'get'):
            return None
        try:
            value = raw_message.get(header_name)
            return str(value) if value else None
        except Exception:  # pragma: no cover — defensive
            return None

    @api.model
    def _dsn_evidence(self, msg_dict, raw_message):
        """Extract delivery-failure signals the spec requires:
        Return-Path, X-Failed-Recipients, Final-Recipient,
        Action failed, Status 5.x.x, Diagnostic-Code — from the raw
        message's headers and MIME parts (core's is_bounce detection
        only covers bounce-alias To, mailer-daemon From and
        multipart/report content-type). Returns dict or False."""
        ev = {}
        if raw_message is not None and hasattr(raw_message, 'walk'):
            try:
                for part in raw_message.walk():
                    ct = part.get_content_type() if hasattr(part, 'get_content_type') else ''
                    if 'delivery-status' in ct:
                        for key in ('Final-Recipient', 'Status', 'Action',
                                    'Diagnostic-Code', 'Remote-MTA'):
                            val = part.get(key)
                            if val and key not in ev:
                                ev[key] = str(val)
                    else:
                        for key in ('X-Failed-Recipients', 'Final-Recipient',
                                    'Status', 'Action', 'Diagnostic-Code'):
                            val = part.get(key)
                            if val and key not in ev:
                                ev[key] = str(val)
            except Exception:  # pragma: no cover — malformed MIME
                pass
        # Return-Path alone is NOT evidence — every mail has one. Only a
        # null sender (<>) or a daemon/bounce localpart marks a DSN.
        rp = self._raw_header(raw_message, 'Return-Path')
        if rp:
            rp_l = rp.strip().lower().strip('<>')
            if not rp_l or 'mailer-daemon' in rp_l or 'postmaster' in rp_l \
                    or 'bounce' in rp_l:
                ev['Return-Path'] = rp
        for key in ('X-Failed-Recipients', 'Final-Recipient', 'Status',
                    'Diagnostic-Code'):
            if key not in ev:
                val = self._raw_header(raw_message, key)
                if val:
                    ev[key] = val
        # strong DSN signals only (a bare Return-Path evidence alone is
        # not a bounce signature — be conservative, never drop genuine mail)
        strong = any(k in ev for k in ('Status', 'Action', 'Diagnostic-Code',
                                       'X-Failed-Recipients', 'Final-Recipient'))
        if not strong and not ev.get('Return-Path'):
            return False
        status = ev.get('Status') or ev.get('Diagnostic-Code') or ''
        ev['permanent'] = bool(re.search(r'\b5\.\d{1,2}\.\d{1,2}\b', status))
        ev['transient'] = bool(re.search(r'\b4\.\d{1,2}\.\d{1,2}\b', status))
        action = ev.get('Action') or ''
        ev['action_failed'] = action.lower().strip() == 'failed'
        diag = ev.get('Diagnostic-Code') or ''
        ev['diagnostic'] = bool(diag.strip()) or ev['permanent'] or ev['transient']
        return ev

    @api.model
    def _auto_reply_evidence(self, msg_dict, raw_message):
        """Auto-Submitted / X-Auto-Reply-Suppress header values, per the
        spec (subject patterns are secondary). Returns the Auto-Submitted
        value (or False)."""
        auto = self._raw_header(raw_message, 'Auto-Submitted')
        if auto and auto.strip().lower().replace('"', '') in _AUTO_SUBMITTED_VALUES:
            return auto.strip().lower()
        suppress = self._raw_header(raw_message, 'X-Auto-Reply-Suppress')
        if suppress:
            values = {v.strip().lower() for v in re.split(r'[,; ]', suppress) if v.strip()}
            if values & set(_AUTO_REPLY_SUPPRESS):
                return 'auto-replied'
        return False

    @api.model
    def _is_mailing_list(self, msg_dict, raw_message, from_l, subject_l):
        for header in _MAILING_LIST_HEADERS:
            if self._raw_header(raw_message, header):
                return True
        if any(d in from_l for d in _DIGEST_SENDERS):
            return True
        if any(re.search(m, subject_l) for m in _DIGEST_SUBJECT_MARKERS):
            return True
        return False

    @api.model
    def _is_system_message(self, from_l, subject_l):
        localpart = from_l.split('@')[0].strip('<>"').lower()
        if localpart not in _SYSTEM_LOCALPARTS:
            return False
        return any(m in subject_l for m in _SYSTEM_SUBJECT_MARKERS)

    @api.model
    def _is_spam_noise(self, subject, from_l):
        haystack = '%s %s' % (subject.lower(), from_l.lower())
        return any(re.search(p, haystack) for p in _SPAM_PATTERNS)

    @api.model
    def _extract_return_date(self, subject, body):
        """Optional return-date extraction for out-of-office mail.
        Returns a date or False. Weekday-only statements ("back Monday")
        are left for the human — too fuzzy for a Date field."""
        haystack = '%s %s' % (subject, re.sub(r'<[^>]+>', ' ', body or ''))
        haystack = re.sub(r'\s+', ' ', haystack)
        for regex in _RETURN_DATE_RES:
            match = regex.search(haystack)
            if not match:
                continue
            tokens = match.group(1).strip().rstrip('.').split()
            if not tokens:
                continue
            month = _MONTHS.get(tokens[0].lower()[:3])
            if not month:
                continue  # weekday name or unparseable — skip
            day = int(re.sub(r'\D', '', tokens[1])) if len(tokens) > 1 else 0
            if not 1 <= day <= 31:
                continue
            year = int(tokens[2].strip(',')) if len(tokens) > 2 else None
            now = fields.Datetime.now()
            if not year:
                year = now.year
                # past date this year → most likely next year (vacation)
                if datetime(year, month, day) < now:
                    year += 1
            try:
                return fields.Date.to_date(datetime(year, month, day).date())
            except ValueError:  # pragma: no cover
                continue
        return False

    # ── Subject/from heuristics (secondary signals) ────────────────

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
    def _ooo_markers(self, subject_l):
        return any(m in subject_l for m in _OOO_BODY_MARKERS)

    @api.model
    def _looks_like_reply(self, subject, references, in_reply_to, body=None):
        if _REPLY_PREFIX_RE.match(subject or ''):
            return True
        # Belt and suspenders: a References/In-Reply-To that somehow
        # missed core's match still means "reply to a known thread".
        if references or in_reply_to:
            return True
        # Quoted-body evidence (PHASE 5): the sender replied and their
        # client stripped the headers (the Cinelli case, PHASE 1 §5).
        if body and self._body_has_quote(body):
            return True
        return False

    @api.model
    def _body_has_quote(self, body):
        b = (body or '').lower()
        if '<blockquote' in b or '&gt;' in b:
            return True
        if 'wrote:' in b or '---original message---' in b:
            return True
        if re.search(r'\bfrom:\s*[^<\n]{2,80}@[^>\n]{2,80}>?\s*\n\s*(?:sent|date|to|cc|subject):', b):
            return True
        return False

    @api.model
    def _recently_contacted(self, msg_dict):
        """PHASE 5 signal: the sender has an outbound message from us in
        the last 90 days → their mail is reply-lookalike, not a brand-new
        thread (the queue operator decides attach vs create)."""
        author_id = msg_dict.get('author_id')
        if not author_id:
            return False
        cutoff = fields.Datetime.now() - timedelta(days=90)
        leads = self.env['crm.lead'].sudo().search([
            ('partner_id', '=', author_id),
            ('date_last_stage_update', '>=', cutoff),
        ])
        for lead in leads:
            outbound = lead.message_ids.filtered(
                lambda m: m.message_type in ('comment', 'email_outgoing')
                and m.author_id and m.author_id.user_ids
                and m.date >= cutoff
            )
            if outbound:
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

    @api.model
    def _find_original_message(self, msg_dict):
        """Stored mail.message referenced by this mail's
        References/In-Reply-To (the mail we sent that came back)."""
        refs = '%s %s' % (msg_dict.get('references') or '',
                          msg_dict.get('in_reply_to') or '')
        for mid in refs.split():
            mid = mid.strip('<>')
            if not mid or 'openerp-' not in mid:
                continue
            # stored ids come in two shapes: native 'openerp-…' (no
            # brackets) and Resend/SES-rewritten '<…-openerp-…>' (with
            # brackets, e.g. lead 961's messages) — match both, as core
            # does (it searches raw reference ids)
            msg = self.env['mail.message'].sudo().search(
                [('message_id', 'in', (mid, '<%s>' % mid)),
                 ('model', '=', 'crm.lead')], limit=1)
            if msg:
                return msg
        return False

    # ── Outcomes ───────────────────────────────────────────────────

    @api.model
    def handle_silent(self, kind, msg_dict, detail=''):
        """Bounce-class noise (mailing_list / system_message /
        spam_or_noise): drop with an audit log line."""
        _logger.info(
            'premafirm.mail.routing: %s%s mail dropped (no lead, no engagement) '
            'from %s to %s, subject "%s", Message-Id %s',
            kind, ' (%s)' % detail if detail else '',
            msg_dict.get('email_from'), msg_dict.get('to'),
            msg_dict.get('subject'), msg_dict.get('message_id'),
        )

    @api.model
    def handle_bounce(self, verdict, msg_dict):
        """Bounce / delivery_failure that core's is_bounce detection
        missed. Spec: no lead, no Replied, no needs_attention; attach the
        delivery failure to the original mail; bounce counter and
        suppression on permanent (5.x.x); preserve evidence."""
        dsn = verdict.get('dsn') or {}
        status = dsn.get('Status') or ''
        diag = dsn.get('Diagnostic-Code') or ''
        final = dsn.get('Final-Recipient') or dsn.get('X-Failed-Recipients') or ''
        # "Final-Recipient: rfc822; user@domain"
        bounced_email = (final.split(';', 1)[1].strip() if ';' in final else final).strip()

        orig_msg = self._find_original_message(msg_dict)
        if orig_msg:
            # native equivalent: mark the notifications of the original
            # mail as bounced (failure attached to the delivery record)
            self.env['mail.notification'].sudo().search([
                ('mail_message_id', '=', orig_msg.id),
            ]).write({
                'failure_reason': diag or status or 'Delivery failure',
                'failure_type': 'mail_bounce',
                'notification_status': 'bounce',
            })
            _logger.warning(
                'premafirm.mail.routing: %s (core is_bounce missed) — original '
                'message %s (crm.lead %s) notifications marked bounced',
                verdict['kind'], orig_msg.id, orig_msg.res_id)

        if verdict['kind'] == 'delivery_failure' and dsn.get('permanent'):
            # bounce counter + suppression on permanent failures only:
            # native blacklist mixin increments message_bounce and
            # suppresses after threshold — reuse it, do not reimplement
            partner = self.env['res.partner'].sudo().search(
                [('email_normalized', '=', email_normalize(bounced_email))], limit=1)
            blacklist = self.env['mail.blacklist'].sudo().search(
                [('email', '=', email_normalize(bounced_email))], limit=1)
            if blacklist:
                blacklist._message_receive_bounce(bounced_email, partner)

        _logger.warning(
            'premafirm.mail.routing: %s dropped (no lead, no engagement) '
            'from %s, subject "%s", Message-Id %s — status=%s recipient=%s diag=%s',
            verdict['kind'], msg_dict.get('email_from'), msg_dict.get('subject'),
            msg_dict.get('message_id'), status or '-', bounced_email or '-',
            diag or '-')

    @api.model
    def queue_inbound(self, verdict, msg_dict):
        """Unmatched reply / OOO / auto-reply → Inbound Review Queue
        record. Never creates a lead."""
        author_id = msg_dict.get('author_id')
        queue = self.env['premafirm.inbound.queue'].sudo().create({
            'name': (verdict.get('subject') or 'No Subject')[:250],
            'email_from': (msg_dict.get('email_from') or '')[:250],
            'partner_id': author_id or False,
            'message_id': msg_dict.get('message_id') or False,
            'mailbox': (msg_dict.get('to') or '')[:250],
            'classification': verdict['kind'],
            'thread_candidate_id': verdict.get('thread_candidate_id') or False,
            'body': msg_dict.get('body') or '',
            'ooo_return_date': verdict.get('ooo_return_date') or False,
        })
        _logger.info(
            'premafirm.mail.routing: %s queued (id %s, thread candidate %s%s) '
            'from %s, subject "%s"',
            verdict['kind'], queue.id, queue.thread_candidate_id.id or 0,
            ', return %s' % queue.ooo_return_date if queue.ooo_return_date else '',
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
        # EVERY crm.lead route is a candidate — including reference-matched
        # routes, which core returns with an EMPTY alias (thread found by
        # In-Reply-To/References, no alias in the mail). Skipping those was
        # a hole: thread-matched OOO/auto-reply/bounce mail went straight
        # onto the lead (Replied stamped, needs_attention raised). The
        # classifier guards them via `matched` so such mail NEVER posts to
        # a lead (no Replied, no needs_attention — automation 60 must not
        # see it).
        svc = self.env['premafirm.mail.routing']
        kept = []
        for route in routes or ():
            _model, tid, _cv, _uid, alias = route
            if _model != 'crm.lead':
                kept.append(route)
                continue
            verdict = svc.classify(message_dict, raw_message=message,
                                   matched=bool(tid))
            kind = verdict['kind']
            if kind in ('new_inquiry', 'normal_reply'):
                # PHASE 8 — stamp reply-status fields at ROUTE time so the
                # gated "CRM: Replied" automation sees them when the post
                # triggers it. Bounces/OOO/auto-replies never reach here
                # (they are queued/absorbed below) so they can never stamp
                # engagement. New-inquiry leads carry their classification
                # in the create values — the automation domain then fails
                # on them (a fresh inquiry is not a reply).
                stamp = {'last_inbound_classification': kind,
                         'last_inbound_at': fields.Datetime.now()}
                if kind == 'normal_reply':
                    stamp['last_meaningful_reply_at'] = stamp['last_inbound_at']
                    # PHASE 10 — a genuine reply answers any open bulk item
                    # on this lead (moves it to 'replied', records the
                    # response message-id). Bounces/OOO never reach here.
                    self.env['premafirm.crm.bulk.email.queue']._mark_replied(
                        tid, response_message_id=message_dict.get('message_id'))
                    # PHASE 11 — the sender is a tracked CONTACT even when
                    # a different contact (not the primary) replies.
                    author = message_dict.get('author_id')
                    if author and tid:
                        self.env['crm.lead.contact']._attach_sender(
                            tid, author)
                if tid:
                    self.env['crm.lead'].sudo().browse(tid).write(stamp)
                else:
                    cv = dict(_cv or {})
                    cv.update(stamp)
                    # PHASE 11 — pass the new-inquiry sender to the lead
                    # create hook (contact row + company-first partner).
                    if kind == 'new_inquiry' and message_dict.get('author_id'):
                        cv['premafirm_attach_contact'] = message_dict['author_id']
                    route = (_model, tid, cv, _uid, alias)
                kept.append(route)
                continue
            if kind == 'unmatched_reply':
                svc.queue_inbound(verdict, message_dict)
            elif kind in ('out_of_office', 'auto_reply'):
                verdict['thread_candidate_id'] = (verdict.get('thread_candidate_id')
                                                  or tid or False)
                svc.queue_inbound(verdict, message_dict)
            elif kind in ('bounce', 'delivery_failure'):
                svc.handle_bounce(verdict, message_dict)
            else:  # mailing_list, system_message, spam_or_noise
                svc.handle_silent(kind, message_dict, detail=verdict.get('detail', ''))
            # absorbed — not kept
        return kept


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
        ('normal_reply', 'Normal Reply'),
        ('new_inquiry', 'New Inquiry'),
        ('unmatched_reply', 'Unmatched Reply'),
        ('bounce', 'Bounce'),
        ('delivery_failure', 'Delivery Failure'),
        ('out_of_office', 'Out of Office'),
        ('auto_reply', 'Auto-Reply'),
        ('mailing_list', 'Mailing List'),
        ('system_message', 'System Message'),
        ('spam_or_noise', 'Spam / Noise'),
        ('internal', 'Internal Noise'),
        ('other', 'Other'),
    ], default='unmatched_reply', required=True, index=True)
    thread_candidate_id = fields.Many2one('crm.lead', 'Likely Thread',
                                          ondelete='set null', index=True,
                                          help='Best guess only — always verify '
                                               'before threading.')
    ooo_return_date = fields.Date('OOO Return Date',
                                  help='Extracted from the out-of-office mail '
                                       '(best effort).')
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

    def action_mark_bounce(self):
        self.write({'classification': 'bounce', 'state': 'reviewed',
                    'review_note': 'Marked as bounce by reviewer.'})
        return True

    def action_mark_ooo(self):
        self.write({'classification': 'out_of_office', 'state': 'reviewed',
                    'review_note': 'Marked as out-of-office by reviewer.'})
        return True

    def action_thread_to_lead(self):
        """Human-confirmed attach: post the stored mail onto the lead's
        chatter as a comment (no message_id — keeps dedupe clean) and
        close the queue record. Only ever called by a human reviewer."""
        for rec in self:
            if not rec.thread_candidate_id:
                continue
            lead = rec.thread_candidate_id
            if rec.classification == 'unmatched_reply':
                # PHASE 8 — a human-confirmed genuine reply counts as
                # meaningful engagement. Bounce/OOO attaches never do.
                now = fields.Datetime.now()
                lead.write({'last_inbound_at': now,
                            'last_meaningful_reply_at': now,
                            'last_inbound_classification': 'normal_reply'})
                # PHASE 10 — the human-confirmed reply answers open bulk
                # items on the lead.
                self.env['premafirm.crm.bulk.email.queue']._mark_replied(
                    lead.id)
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
                # PHASE 8 — the reviewer confirmed this unmatched mail is a
                # genuine inquiry: it counts as inbound engagement, never as
                # a reply.
                'last_inbound_classification': 'new_inquiry',
                'last_inbound_at': fields.Datetime.now(),
            })
            lead.message_post(
                body=rec.body or '<p>(no body captured)</p>',
                subject='Imported from Inbound Review Queue: %s' % (rec.name or ''),
                subtype_xmlid='mail.mt_comment',
            )
            rec.write({'state': 'lead_created', 'review_note': 'Lead %s created' % lead.id})
        return True
