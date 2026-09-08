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
from datetime import date, datetime, timedelta

import pytz
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
    - A trailing sign-off block the top-down cut missed ("Thanks,\\nAhmad",
      "Kind regards,\\nAhmad Ibrahim", ...) — see _strip_trailing_signoff.
    """
    lines = text.replace('\r\n', '\n').split('\n')
    body_lines = []
    sig_triggers = re.compile(
        r'^(--|(?:best|kind|warm|many|my|simple)\s+regards?|regards,|'
        r'best wishes|best,|sincerely|warmly|cheers,|yours '
        r'(?:truly|sincerely|faithfully)|cordially|respectfully|'
        r'ahmad ibrahim|premafirm|owner.?operator)',
        re.IGNORECASE,
    )
    for line in lines:
        stripped = line.strip()
        if re.match(r'^subject\s*:', stripped, re.IGNORECASE):
            continue
        if sig_triggers.match(stripped):
            break
        body_lines.append(line)
    return _strip_trailing_signoff('\n'.join(body_lines).strip())


# Sign-off heads the model appends despite the draft rules ("never end with a
# sign-off line").  Each form is checked independently: a single alternation
# would let "thanks?" swallow the prefix of "Thank you," and never reach the
# longer form.
_SIGNOFF_HEADS = (
    r'with\s+(?:best|kind|warm|many)?\s*regards?',
    r'(?:best|kind|warm|many|my|simple)?\s*regards?',
    r'best\s+wishes',
    r'many\s+thanks?',
    r'thanks?\s+(?:so\s+)?much',
    r'thank\s+you\s+(?:so\s+)?much',
    r'thanks?',
    r'thank\s+you',
    r'best',
    r'sincerely',
    r'warmly',
    r'cheers',
    r'thx',
    r'yours\s+(?:truly|sincerely|faithfully)',
    r'cordially',
    r'respectfully',
    r'have\s+a\s+(?:great|good|nice|wonderful|lovely)\s+'
    r'(?:day|weekend|one|afternoon|evening|morning)',
)
_SIGNOFF_HEADS_RE = [re.compile(f'^{p}', re.IGNORECASE) for p in _SIGNOFF_HEADS]

# "Ahmad Ibrahim", "PremaFirm Inc.", "Owner/Operator", "OdooBot" — but never a
# prose sentence (stop words reject "for your help", an interior '.' rejects
# "quick reply.", a comma rejects "See you, ...").  "Inc." is the one allowed
# trailing dot.  Closing words ("regards", "thanks" ...) are stop words so a
# bare "Best regards" line is recognized as a sign-off head, not a name.
_NAME_LINE_RE = re.compile(r"[A-Za-z0-9 &'/-]+")
_NAME_STOPWORDS = {
    'for', 'the', 'your', 'you', 'and', 'to', 'a', 'of', 'on', 'in', 'with',
    'my', 'our', 'so', 'much', 'again', 'very', 'all', 'this', 'that', 'it',
    'we', 'will', 'please', 'let', 'know', 'have', 'has', 'from', 'at', 'be',
    'best', 'regards', 'regard', 'wishes', 'thanks', 'thank', 'cheers', 'thx',
    'sincerely', 'warmly', 'yours', 'truly', 'faithfully', 'cordially',
    'respectfully',
}


def _is_name_line(s):
    s = s.strip()
    if not 1 <= len(s) <= 45:
        return False
    if not re.search(r'[A-Za-z]', s):
        return False
    if '!' in s or '?' in s or s.count('.') > 1:
        return False
    if '.' in s and not s.endswith('.'):
        return False
    if not _NAME_LINE_RE.fullmatch(s):
        return False
    return not any(w in _NAME_STOPWORDS for w in s.lower().split())


def _is_signoff_head(s):
    s = s.strip()
    for pat in _SIGNOFF_HEADS_RE:
        m = pat.match(s)
        if not m:
            continue
        rest = re.sub(r'^[,.;:!]+', '', s[m.end():]).strip()
        if not rest:
            return True  # "Best regards", "Thanks so much!", "Have a great day"
        if _is_name_line(rest):
            return True  # "Best, Ahmad" / "Kind regards, Ahmad Ibrahim"
        # A longer head may still fit ("thank" was a prefix of "thank you,");
        # keep trying the remaining forms.
    return False


def _strip_trailing_signoff(text):
    """Remove a trailing sign-off block from the END of the text only.

    The top-down trigger cut cannot catch the thanks-family ("Thanks,\\nAhmad"
    would survive because "Thanks for your help" inside the body legitimately
    starts with the same word), so this pass works backwards: it walks up over
    blank, name and sign-off-head lines until it hits real content, then peels
    the block only when its top line is a sign-off head ("Best regards,\\n
    OdooBot", "Regards,\\nAhmad Ibrahim\\nOwner/Operator", "Thanks, Ahmad",
    a bare "Have a great weekend").  Interior content is never touched, and a
    final name line with no sign-off head above it is left alone.
    """
    lines = text.replace('\r\n', '\n').split('\n')
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    top = None
    i = end - 1
    while i >= 0:
        s = lines[i].strip()
        if not s:
            i -= 1
            continue
        if _is_name_line(s) or _is_signoff_head(s):
            top = i
            i -= 1
            continue
        break
    if top is None or not _is_signoff_head(lines[top].strip()):
        return text
    if not all(not lines[k].strip()
               or _is_name_line(lines[k].strip())
               or _is_signoff_head(lines[k].strip())
               for k in range(top + 1, end)):
        return text
    out = '\n'.join(lines[:top]).rstrip()
    return out if out.strip() else text


def _draft_email_only(text):
    """Collapse a model draft to its canonical stored form.

    A draft answer is stored as exactly what the composer will send: a single
    "SUBJECT: ..." line followed by the email body, with any stray subject
    lines, signature blocks, '---' postscripts or sign-off lines removed.  If
    nothing survives (the model replied with only a signature), the original
    text is kept rather than a shell of the email.
    """
    lines = text.replace('\r\n', '\n').split('\n')
    subject = None
    body_start = 0
    for i, line in enumerate(lines):
        m = re.match(r'^subject\s*:\s*(.+)$', line.strip(), re.IGNORECASE)
        if m and m.group(1).strip():
            subject = m.group(1).strip()
            body_start = i + 1
            break
    body = _strip_ai_meta('\n'.join(lines[body_start:])) \
        if body_start < len(lines) else ''
    if not body.strip():
        return text
    if subject:
        return f'SUBJECT: {subject}\n\n{body}'.strip()
    return body


def _gpt(env, system, messages, max_tokens=800):
    from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat
    key = _api_key(env)
    if not key:
        raise ValueError("DeepSeek API key not configured.")
    return deepseek_chat(messages=messages, system=system, max_tokens=max_tokens, api_key=key)


# ── Ask-AI: draft-request classification ─────────────────────────────────────
# A "draft" request asks the AI to produce an email/message the user can send;
# everything else (analysis, advice, reviews) goes through the general prompt.
# The negative lookahead keeps rhetorical questions ("why should I email?")
# from being misclassified as drafting instructions.
_DRAFT_REQUEST_RE = re.compile(
    r'\b(draft|write|compose|send|prepare|create|re-?send|reply|respond|'
    r'answer|email)\b'
    r'(?:(?!\bwhy\b|\bhow\b|\bshould\b|\bwould\b|\bcan\b|\bwhat\b).){0,80}'
    r'\b(e-?mail|mail|message|note|follow[- ]?up|followup|subject|thread|'
    r'reply|voicemail|her|him|them)\b'
    r'|\b(touch base|reach(?:ing)? out|get back to)\b',
    re.IGNORECASE | re.DOTALL,
)

# The user asked the AI to *check* account history (notes/thread/calls) — used
# with the retrieval guard so we say so when nothing was retrievable.
_HISTORY_CHECK_RE = re.compile(
    r'\b(check|review|look|read|consult|refer|use|see)\b.{0,50}'
    r'\b(notes?|history|thread|chatter|log(?:ged)?|calls?|emails?|activity)\b'
    r'|\b(notes?|history|thread|chatter|logs?)\b.{0,50}'
    r'\b(check|review|look|see|read)\b',
    re.IGNORECASE | re.DOTALL,
)

_HISTORY_UNAVAILABLE_NOTE = (
    "\n\n---\nNote for Ahmad (not part of the email): I could not retrieve any "
    "email thread, internal notes, activities, meetings or call records for "
    "this lead, so the draft above is based only on your message. I have not "
    "pretended to check history I could not see."
)


def _user_tz_name(env):
    """Timezone of the logged-in user; Eastern is the operating default.

    Odoo 18 keeps the tz on res.users' partner; fall back so drafts and
    timestamps stay coherent even for users without a zone configured.
    """
    try:
        user = env.user
        return (user.tz
                or (user.partner_id.tz if user.partner_id else '')
                or 'America/Toronto')
    except Exception:
        return 'America/Toronto'


def _localize(dt, tz_name):
    """Convert an Odoo (naive UTC) datetime to the user's zone, naive-local."""
    if not dt:
        return None
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone('America/Toronto')
    if getattr(dt, 'tzinfo', None) is not None:
        return dt.astimezone(tz).replace(tzinfo=None)
    return pytz.utc.localize(dt).astimezone(tz).replace(tzinfo=None)


def _now_local(tz_name):
    """Current wall-clock time in the user's zone, naive-local."""
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone('America/Toronto')
    return datetime.now(pytz.utc).astimezone(tz).replace(tzinfo=None)


def _when(dt, tz_name, today=None):
    """Human label for a naive-UTC datetime: 'YYYY-MM-DD HH:MM local (rel)'."""
    loc = _localize(dt, tz_name)
    if loc is None:
        return '?'
    if today is None:
        today = _now_local(tz_name).date()
    day = loc.date()
    if day == today:
        rel = 'today'
    elif day == today - timedelta(days=1):
        rel = 'yesterday'
    elif day < today:
        rel = f'{abs((today - day).days)} days ago'
    else:
        rel = f'in {(day - today).days} days'
    return f'{loc.strftime("%Y-%m-%d %H:%M")} local ({rel})'


def _when_date(d, tz_name, today=None):
    """Relative label for a plain date (invoice dates etc.)."""
    if not d:
        return '?'
    if today is None:
        today = _now_local(tz_name).date()
    if d == today:
        rel = 'today'
    elif d == today - timedelta(days=1):
        rel = 'yesterday'
    elif d < today:
        rel = f'{abs((today - d).days)} days ago'
    else:
        rel = f'in {(d - today).days} days'
    return f'{d.strftime("%Y-%m-%d")} ({rel})'


def _today_context_line(tz_name):
    from odoo.addons.premafirm_ai_engine.services.deepseek_utils import (
        today_context_line,
    )
    try:
        return today_context_line(tz_name)
    except Exception:
        return today_context_line()


def _profile_company_context(env):
    """Company facts for DRAFTS — business-profile fields ONLY.

    Deliberately excludes the knowledge-base documents and the hard-coded
    fallback text (whose lanes/credentials lists are not maintained as
    'current company information') so the AI can never auto-claim
    cross-border authority, MC/USDOT numbers, insurance figures or lanes the
    operator has not recorded in the profile.
    """
    parts = ['=== COMPANY CONTEXT ===']
    try:
        profile = env['premafirm.business.profile'].sudo().get_profile()
    except Exception:
        profile = None
    if not profile:
        parts.append('(no company profile configured — claim nothing about '
                     'the company beyond what the user states)')
        return '\n'.join(parts)
    parts.append(f'Company: {profile.company_name or "PremaFirm Inc."}')
    for label, value in (
            ('Overview', profile.company_overview),
            ('Services', profile.services_description),
            ('Key differentiators', profile.key_differentiators),
            ('Pricing context', profile.pricing_context),
            ('Team', profile.team_info),
    ):
        if value:
            parts.append(f'\n{label}:\n{value}')
    if profile.tone_of_voice:
        parts.append(f'\nTone of voice: '
                     f'{dict(profile._fields["tone_of_voice"].selection).get(profile.tone_of_voice, profile.tone_of_voice)}')
    parts.append('\nOnly facts stated above may ever be claimed about '
                 'PremaFirm in a draft.')
    return '\n'.join(parts)


# Drafting rules for the Ask-AI widget — code-side, so they apply even where
# the editable ai_role_prompt stored in the DB predates these rules.
_DRAFT_RULES = r'''
=== GROUND-TRUTH RULES (MUST FOLLOW) ===
1. The user's REQUEST below is ground truth: it happened exactly as stated.
   Restate those facts faithfully. Never embellish, soften, or add to them.
2. INVENT NOTHING. Never invent conversations, meetings, calls, emails,
   dates, referrals, promises, interest, requirements, availability, or
   customer statements that the REQUEST or ACCOUNT CONTEXT does not contain.
3. Absence is not an event. "Anna is not in today" does NOT mean you spoke
   with Anna, that Anna told you anything, or that you know why she is out.
   "I tried calling Victoria today and reached voicemail" does NOT mean the
   call connected, that you left a message, or that you ever reached her.
4. Never claim you spoke with, emailed, or met anyone unless ACCOUNT CONTEXT
   shows a real dated message/call with that person, or the REQUEST says so.
5. Keep history anchored to its own dates. An entry stamped weeks ago is NOT
   current just because it reads like it; never present old events as fresh,
   and never resolve "today/yesterday/tomorrow/last week" by guessing — use
   the CURRENT DATE line, which is the real current date in the user's zone.

=== PRIVACY OF INTERNAL NOTES ===
6. INTERNAL NOTES (opportunity description, chatter notes, contact/company
   logs, call dispositions) are private background context for your
   understanding ONLY. NEVER quote, paraphrase, or hint at them in the
   email — never write "our notes show", "I saw in your file", "according to
   our records" or "I understand you...". The customer only learns what the
   user's REQUEST states.
7. If the user says "check the internal notes", use them to understand the
   situation and shape the email — the email still only carries the user's
   own statements.
8. Never claim you checked history that is not in ACCOUNT CONTEXT, and never
   comment on the history at all inside the email. If the user asked you to
   check notes/history and none could be retrieved, a "---" note is appended
   automatically AFTER your reply by the system — you must not write it
   yourself, and you must not pretend you reviewed anything.

=== CONTINUING THE REAL CONVERSATION ===
9. Address exactly the person the user names in the REQUEST — even when the
   lead's own contact is someone else. Address nobody else, copy nobody else.
10. Review the EMAIL THREAD section first. If a real exchange exists,
    continue it: same subject context, no repeated introduction, no
    re-asking questions already answered, no re-introducing PremaFirm to a
    company that already knows it.
11. If the REQUEST states a person is unavailable ("Anna is not in today",
    "out of the office", "off today", "on leave") and the email is addressed
    to that person's colleague, you MUST include that fact in the email body
    — it is the stated reason for writing to them instead of the unavailable
    person, and omitting it leaves the email inexplicable. Including it is
    not an embellishment: it is a ground-truth fact from the REQUEST (rule 1).
    State ONLY the fact the user gave: do not explain why, do not name a
    source, do not attribute it, do not guess when they return (rule 3 —
    absence is not contact: never imply you spoke with the person).

=== COMPANY CLAIMS ===
12. Only mention PremaFirm services, lanes, equipment, capacity, rates,
    authority numbers, insurance, or Canada-USA cross-border capability that
    (a) appear verbatim in COMPANY CONTEXT above AND (b) the user's request
    actually needs. Otherwise omit. Never add claims the profile does not
    support, and never volunteer cross-border or credentials unprompted.

=== AMBIGUITY / CONFLICT ===
13. The user's latest explicit words override any older context row. If
    drafting would require an important guess (recipient's email absent and
    not given, two context entries irreconcilably conflict, the person named
    cannot be identified), do NOT guess: reply with exactly one short line
    starting "QUESTION: " asking what you need.
14. If the REQUEST turns out to be a question rather than an instruction to
    draft, answer it briefly and factually instead of drafting.

=== OUTPUT FORMAT (email requests) ===
15. Output ONLY the email:
    Line 1: SUBJECT: <one concise subject>
    Blank line.
    Body starting "Hi <FirstName>," — brief (under ~110 words), plain text,
    no markdown, no bullet lists unless the situation truly needs one.
16. Nothing before the subject. No "Objective", "Account insight",
    "Recommended next action", analysis, review, or notes after the body. No
    signature or contact block — the email system appends the signature. That
    includes any closing sign-off line: never end with "Best regards",
    "Thanks", your name, or the sender's name — finish at the last content
    sentence.
17. Never append anything after the email yourself — no "---" line, no note,
    no commentary about the history you found or did not find, no hedging.
    Your reply ends when the email ends. (If a check genuinely found nothing,
    a "---" note appears automatically on its own; do not duplicate or
    pre-empt it. If an important decision is missing, use the QUESTION: line
    from rule 13 INSTEAD of the email.)
'''

# Discipline block appended to the general (non-draft) mode.  The legacy
# editable role prompt tells the model to "connect dots across all history
# automatically", which is exactly what makes it invent prior conversations —
# these code-side rules outrank that instruction by coming after it.
_FACT_DISCIPLINE_RULES = r'''
=== FACT DISCIPLINE (ALWAYS) ===
- The user's REQUEST is ground truth. Never invent conversations, dates,
  referrals, promises, requirements, interest, or availability beyond it and
  beyond the dated history present in ACCOUNT CONTEXT.
- Absence is not an event: "X is not in today" never implies a prior
  conversation with X; a voicemail is not a conversation and not a message
  left.
- Keep every historical item anchored to its own timestamp; use the CURRENT
  DATE line to interpret today/yesterday/tomorrow. Never present old events
  as current.
- INTERNAL NOTES are private: never quote them to customers, never attribute
  anything to notes, and never claim you checked history that is not present
  in ACCOUNT CONTEXT — say so instead.
- Only mention services, lanes, credentials, insurance, or cross-border
  capability that the COMPANY CONTEXT in your system prompt actually
  supports, and only when relevant to the request.
- When asked for a brief email or message, output ONLY a "SUBJECT:" line and
  the body — no other sections.
- If an important ambiguity remains, ask ONE short question rather than
  guessing.
'''

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
            is_draft = bool(_DRAFT_REQUEST_RE.search(user_input))
            system = (self._ai_draft_system_prompt() if is_draft
                      else self._ai_system_prompt())
            context = self._ai_lead_context()
            response = _gpt(self.env, system, [{
                'role': 'user',
                'content': f'ACCOUNT CONTEXT:\n{context}\n\nREQUEST:\n{user_input}',
            }], max_tokens=1200)
            if is_draft and response:
                response = self._draft_post_checks(response, user_input)
            self.sudo().write({'x_ai_chat_response': response})
        except Exception as exc:
            self.sudo().write({'x_ai_chat_response': f'⚠ {exc}'})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _draft_post_checks(self, response, user_input):
        """Deterministic guards on a generated draft (the model rules cover
        the same ground; these make them unforgeable).

        * The draft is normalized to "SUBJECT: ... + email body" — the exact
          form the composer sends — so a sign-off line ("Best regards,
          OdooBot"), a stray signature or a '---' postscript the model wrote
          never appears in the stored chat answer either.
        * If the user explicitly asked us to check notes/history and NO
          history at all was retrievable, append the '---' note so it is
          visible in the chat but never lands in the composed email (the
          composer truncates the body at a '---' separator).
        * A bare "QUESTION: ..." answer is left untouched so the user can
          answer it in the same box — never wrapped in draft framing.
        """
        if response.strip().startswith('QUESTION'):
            return response
        response = _draft_email_only(response)
        asked_for_history = bool(_HISTORY_CHECK_RE.search(user_input))
        if asked_for_history:
            stats = self._ai_history_stats()
            if not any(stats.values()):
                if '\n---' not in response:
                    response += _HISTORY_UNAVAILABLE_NOTE
        return response

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

    @api.returns('mail.message', lambda value: value.id)
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

    def _ai_draft_system_prompt(self):
        """System prompt for requests that ask for a DRAFTED email/message.

        Built in code (NOT from the editable ai_role_prompt, whose legacy
        text demands analysis sections and 'connect-the-dots' narration —
        exactly what made brief emails drift into invented history).
        Company context comes from the operator-maintained business-profile
        fields only, so lanes/credentials/cross-border are never auto-claimed
        beyond what the profile states as current.
        """
        tz_name = _user_tz_name(self.env)
        user = self.env.user
        parts = [_profile_company_context(self.env)]
        parts.append(_DRAFT_RULES)
        parts.append(
            '=== WHO YOU ARE DRAFTING FOR ===\n'
            f'You are drafting on behalf of {user.name or "the logged-in user"} '
            f'(the person writing the REQUEST). Sign off and self-identify as '
            f'that person — never as Ahmad Ibrahim unless the REQUEST comes '
            f'from Ahmad Ibrahim. Do not include any signature, title block '
            f'or contact details in the email; the email system appends the '
            f'signature itself.'
        )
        parts.append(
            '=== CURRENT DATE ===\n'
            + _today_context_line(tz_name)
            + f' All timestamps in ACCOUNT CONTEXT below are shown in this '
              f'same timezone ({tz_name}) and marked "local". Treat "today", '
              f'"yesterday" and "tomorrow" in the REQUEST strictly against '
              f'the date above — never a guessed one.'
        )
        return '\n\n'.join(parts)

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
        # Real "today" in the user's own timezone — the model must never guess
        # the current date when resolving relative words in the request.
        tz_name = _user_tz_name(self.env)
        prompt += ('\n\n=== CURRENT DATE ===\n'
                   + _today_context_line(tz_name)
                   + f' All timestamps in ACCOUNT CONTEXT are shown in this '
                     f'timezone ({tz_name}) and marked "local".')
        # Code-side fact discipline — appended AFTER the editable role prompt
        # so its "connect the dots" style cannot override ground truth.
        prompt += '\n\n' + _FACT_DISCIPLINE_RULES
        return prompt

    def _ai_lead_context(self):
        """Build a dated 360° account context for the AI.

        Design rules (ASK-AI fix wave):
        * Every historical entry carries its LOCAL date in the user's
          timezone plus a relative label, so 'today/yesterday' in the
          request can never re-date old events.
        * Real emails, chatter/internal notes, activities, meetings and call
          records are reported in SEPARATE clearly-labelled sections, so the
          model can tell an actual conversation from an internal note.
        * The opportunity description and lead-level chatter are included
          (they were previously only visible when they happened to sit in
          the top-12 message mix), while tracking/stage-change noise is
          excluded.
        """
        parts = []
        partner = self.partner_id
        company = self._ai_company_partner()
        contact_name = partner.name if partner else self.partner_name or 'Unknown'
        company_name = company.name if company else contact_name
        tz_name = _user_tz_name(self.env)
        today = _now_local(tz_name).date()

        # ── Date anchor ─────────────────────────────────────────────────────
        parts.append('=== DATE CONTEXT ===')
        parts.append(_today_context_line(tz_name))
        parts.append(f'All timestamps below are shown in this timezone '
                     f'({tz_name}) and marked "local". Older entries are old '
                     'even when they read like the current situation.')

        # ── Lead snapshot ───────────────────────────────────────────────────
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
            parts.append('Last outreach: '
                         f'{_when(self.x_last_outreach_at, tz_name, today)}')
        referred = getattr(self, 'x_referred_by_partner_id', None)
        if referred:
            parts.append(f'Referred by: {referred.name}'
                         + (f' ({referred.function})' if referred.function else ''))

        # ── Stage change history ────────────────────────────────────────────
        stage_changes = []
        try:
            for msg in self.message_ids.sorted('date'):
                for tv in msg.sudo().tracking_value_ids:
                    try:
                        if tv.field_id.name == 'stage_id':
                            ts = _when(msg.date, tz_name, today) if msg.date else '?'
                            old = tv.old_value_char or '?'
                            new = tv.new_value_char or '?'
                            stage_changes.append(f'[{ts}] {old} → {new}')
                    except Exception:
                        pass
        except Exception:
            pass
        if stage_changes:
            parts.append('Stage history: ' + ' | '.join(stage_changes[-6:]))

        # ── Lead internal description ───────────────────────────────────────
        try:
            desc = _strip_html(self.description or '')[:500]
            if desc:
                parts.append('\n=== LEAD INTERNAL DESCRIPTION ===')
                parts.append(desc)
        except Exception:
            pass

        # ── Lead chatter / internal notes ───────────────────────────────────
        lead_notes = []
        try:
            for msg in self.message_ids.sorted('date', reverse=True):
                if msg.message_type != 'comment':
                    continue
                if msg.tracking_value_ids:
                    continue  # stage-change noise (tracked in Stage history)
                body = _strip_html(msg.body)
                if not body or len(body) < 15:
                    continue
                author = msg.author_id.name if msg.author_id else '?'
                lead_notes.append(f'[{_when(msg.date, tz_name, today)} local] '
                                  f'{author}: {body[:400]}')
                if len(lead_notes) >= 12:
                    break
        except Exception:
            pass
        if lead_notes:
            parts.append('\n=== LEAD CHATTER / INTERNAL NOTES (newest first) ===')
            parts.extend(lead_notes)

        # ── Email thread (real emails only) ─────────────────────────────────
        emails = []
        try:
            for msg in self.message_ids.sorted('date', reverse=True):
                if msg.message_type != 'email':
                    continue
                body = _strip_html(msg.body)
                if not body or len(body) < 10:
                    continue
                if msg.author_id and msg.author_id.user_ids:
                    to = ', '.join(p.name for p in msg.partner_ids[:3]) or '?'
                    direction = f'→ SENT to {to}'
                    author = 'us'
                else:
                    direction = '← RECEIVED from'
                    author = msg.author_id.name if msg.author_id else '?'
                subject = (msg.subject or '').strip()
                head = f'[{_when(msg.date, tz_name, today)} local] {direction} {author}'
                if subject:
                    head += f' — "{subject}"'
                emails.append(f'{head}: {body[:450]}')
                if len(emails) >= 15:
                    break
        except Exception:
            pass
        parts.append('\n=== EMAIL THREAD (newest first) ===')
        if emails:
            parts.extend(emails)
        else:
            parts.append('(no emails found yet)')

        # ── Open activities & follow-up tasks ───────────────────────────────
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

        # ── Meetings / calls ────────────────────────────────────────────────
        try:
            meetings = self.env['calendar.event'].sudo().search(
                [('opportunity_id', '=', self.id)], limit=5, order='start desc'
            )
            if meetings:
                parts.append('\n=== MEETINGS ===')
                for m in meetings:
                    ts = _when(m.start, tz_name, today) if m.start else '?'
                    attendees = ', '.join(m.partner_ids.mapped('name')[:4])
                    parts.append(f'[{ts} local] {m.name} — attendees: {attendees}')
        except Exception:
            pass
        try:
            calls = self.env['voipms.call.log'].sudo().search(
                [('lead_id', '=', self.id)], limit=8, order='date desc'
            )
            if calls:
                parts.append('\n=== LOGGED PHONE CALLS ===')
                for c in calls:
                    who = c.partner_id.name if c.partner_id else (c.caller_number or '?')
                    parts.append(
                        f'[{_when(c.date, tz_name, today)} local] '
                        f'{c.direction} call to/from {who} — '
                        f'{c.call_status} — {c.duration or 0}s'
                    )
        except Exception:
            pass

        # ── Contact profile ─────────────────────────────────────────────────
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

        # ── Company profile ─────────────────────────────────────────────────
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

        # ── ALL contacts at this company ────────────────────────────────────
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

        # ── Contact log notes ───────────────────────────────────────────────
        if partner and not partner.is_company:
            cnotes = [n for n in partner.message_ids.sorted('date', reverse=True)
                      if n.message_type == 'comment' and _strip_html(n.body)][:8]
            if cnotes:
                parts.append('\n=== CONTACT LOG NOTES ===')
                for n in cnotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{_when(n.date, tz_name, today)} local]: {b[:400]}')

        # ── Company log notes ───────────────────────────────────────────────
        if company:
            anotes = [n for n in company.message_ids.sorted('date', reverse=True)
                      if n.message_type == 'comment' and _strip_html(n.body)][:10]
            if anotes:
                parts.append('\n=== COMPANY LOG NOTES ===')
                for n in anotes:
                    b = _strip_html(n.body)
                    if b:
                        parts.append(f'[{_when(n.date, tz_name, today)} local]: {b[:400]}')

        # ── Invoice history ─────────────────────────────────────────────────
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
                    parts.append(
                        f'[{_when_date(inv.invoice_date, tz_name, today)}] {inv.name} — '
                        f'${inv.amount_total:,.0f} {inv.currency_id.name} — '
                        f'{lines_summary or "no line detail"}'
                    )
                total_rev = sum(inv.amount_total for inv in invoices)
                parts.append(f'Total from last {len(invoices)} invoices: ${total_rev:,.0f}')

        # ── Other leads for this account ────────────────────────────────────
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
                    ts = _when(l.x_last_outreach_at, tz_name, today) if l.x_last_outreach_at else 'no outreach'
                    won_lost = ' ✅ WON' if l.active and getattr(l, 'probability', 0) == 100 else \
                               (' ❌ LOST' if not l.active else '')
                    contact_on_lead = l.partner_id.name if l.partner_id else '?'
                    parts.append(
                        f'[{l.stage_id.name if l.stage_id else "?"}]{won_lost} '
                        f'Contact: {contact_on_lead} — {l.name} — '
                        f'last outreach: {ts} — reply: {l.x_response_status or "none"}'
                    )

        text = '\n'.join(parts)
        # Hard ceiling so a history-heavy account can never blow the prompt
        if len(text) > 24000:
            text = text[:24000] + '\n[ACCOUNT CONTEXT TRUNCATED — oldest entries omitted]'
        return text

    def _ai_history_stats(self):
        """Counts of what the context builder actually retrieved — used by the
        'say so rather than pretend' guard in _draft_post_checks.

        The discipline module auto-schedules boilerplate activities on every
        lead (Initial Contact, Follow-up ... see _TYPE_XMLIDS). Those are
        pipeline scaffolding, not retrieved history: counting them would
        silence the honest 'no history could be retrieved' note on a fresh
        lead, which is exactly the case the note exists for.
        """
        stats = {'emails': 0, 'notes': 0, 'activities': 0, 'meetings': 0,
                 'calls': 0}
        try:
            msgs = self.message_ids
            stats['emails'] = len([m for m in msgs if m.message_type == 'email'])
            stats['notes'] = len([m for m in msgs
                                  if m.message_type == 'comment'
                                  and not m.tracking_value_ids])
            activities = self.env['mail.activity'].sudo().search([
                ('res_model', '=', 'crm.lead'), ('res_id', '=', self.id)])
            scaffold_ids = self._ai_discipline_activity_type_ids()
            stats['activities'] = len([
                a for a in activities
                if a.activity_type_id.id not in scaffold_ids])
            stats['meetings'] = self.env['calendar.event'].sudo().search_count([
                ('opportunity_id', '=', self.id)])
            stats['calls'] = self.env['voipms.call.log'].sudo().search_count([
                ('lead_id', '=', self.id)])
        except Exception:
            pass
        return stats

    def _ai_discipline_activity_type_ids(self):
        """Ids of the discipline module's auto-scheduled activity types.

        Lazy import (module convention) of the spec dict that the discipline
        model itself uses, so the exclusion can never drift from what the
        automation actually schedules. Types are matched by their xmlids;
        any row deleted or not yet loaded is skipped.
        """
        ids = set()
        try:
            from odoo.addons.premafirm_ai_engine.models import (
                crm_activity_discipline,
            )
            for xmlid in crm_activity_discipline._TYPE_XMLIDS.values():
                try:
                    ttype = self.env.ref(xmlid, raise_if_not_found=False)
                    if ttype:
                        ids.add(ttype.id)
                except Exception:
                    pass
        except Exception:
            pass
        return ids

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
