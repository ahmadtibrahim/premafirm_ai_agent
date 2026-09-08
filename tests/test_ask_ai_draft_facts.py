"""ASK-AI drafting guarantees (2026-09-08 wave).

Regression for the "Anna / Victoria" fabrication: the Ask-AI widget used to
hand the model an undated context and an editable role prompt that demanded
"Objective / Account insight / Recommended next action" sections — so a brief
email request came back with invented prior conversations ("I reached out to
Anna yesterday", "she mentioned she'd be out today").  These tests pin the
fixes:

* draft requests get a code-built system prompt (ground-truth + email-only
  output rules) instead of the legacy role prompt;
* the account context separates real emails from chatter/internal notes and
  stamps every entry with a LOCAL date, so nothing can be re-dated to today;
* an explicit request to check history that could not be retrieved is
  reported honestly (after a '---' separator, never inside the email);
* general (non-draft) requests keep the role prompt but gain the code-side
  fact-discipline block appended AFTER it.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
from odoo.tests import TransactionCase, tagged

DRAFT_RESPONSE = (
    'SUBJECT: Anna is out of the office today\n\n'
    'Hi Victoria,\n\n'
    'I understand Anna is not in today. I tried calling you earlier '
    'today and reached your voicemail — please give me a call back '
    'when you have a moment.\n\n'
    'Best regards,\nAhmad'
)

EXAMPLE_REQUEST = (
    'Anna is not in today. Write a brief email to Victoria. Check the '
    'internal notes. I tried calling her today but reached voicemail.'
)

# (text, is_draft) battery — keep rhetorical questions in the general lane.
CLASSIFICATION = [
    (EXAMPLE_REQUEST, True),
    ('Draft a follow-up email referencing our last invoice', True),
    ('Write a rate increase email — be diplomatic', True),
    ('Reply to Victoria last email about lanes', True),
    ('Send an intro email to the purchasing manager', True),
    ('Review this account and tell me what to do next', False),
    ('Why have they not replied? What is the best approach?', False),
    ('Should I follow up with this company?', False),
    ('What lanes should I pitch to this company?', False),
    ('Analyze why we keep losing these deals', False),
]

TOR = pytz.timezone('America/Toronto')


def days_ago_utc(days):
    """A naive-UTC datetime whose TORONTO date is exactly `days` ago —
    deterministic regardless of the hour the suite runs at."""
    now_local = datetime.now(pytz.utc).astimezone(TOR).replace(tzinfo=None)
    return TOR.localize(now_local - timedelta(days=days)) \
             .astimezone(pytz.utc).replace(tzinfo=None)


@tagged('ask_ai')
class TestAskAiDraftFacts(TransactionCase):
    """End-to-end behavior of the Ask-AI widget with a mocked LLM."""

    def setUp(self):
        super().setUp()
        self.company = self.env['res.partner'].create({
            'name': 'AskAI Test Co', 'is_company': True})
        self.anna = self.env['res.partner'].create({
            'name': 'Anna Smith', 'email': 'anna@askai.example',
            'parent_id': self.company.id, 'function': 'Purchasing'})
        self.victoria = self.env['res.partner'].create({
            'name': 'Victoria Lee', 'email': 'victoria@askai.example',
            'parent_id': self.company.id, 'function': 'Logistics Manager'})
        self.lead = self.env['crm.lead'].create({
            'name': 'AskAI Test Lead',
            'partner_id': self.anna.id,
            'description': '<p>Weekly LTL candidate — Victoria is the '
                           'logistics contact.</p>'})
        self.author = self.env.user.partner_id
        # Deterministic timezone for date labelling assertions.
        self.env.user.tz = 'America/Toronto'

    def _post_note(self, body, dt=None):
        self.env['mail.message'].sudo().create({
            'model': 'crm.lead', 'res_id': self.lead.id,
            'message_type': 'comment',
            'subtype_id': self.env.ref('mail.mt_note').id,
            'author_id': self.author.id,
            'body': f'<p>{body}</p>',
            'date': dt or datetime.utcnow(),
        })

    def _post_email(self, subject, body, from_partner, dt=None):
        self.env['mail.message'].sudo().create({
            'model': 'crm.lead', 'res_id': self.lead.id,
            'message_type': 'email',
            'subtype_id': self.env.ref('mail.mt_comment').id,
            'author_id': from_partner.id,
            'subject': subject, 'body': f'<p>{body}</p>',
            'date': dt or datetime.utcnow(),
        })

    def _capture_gpt(self, answer=DRAFT_RESPONSE):
        """Patch _gpt, returning a canned draft and recording the prompt."""
        captured = {}

        def fake_gpt(env, system, messages, max_tokens=800):
            captured['system'] = system
            captured['messages'] = messages
            return answer

        patch_ctx = patch(
            'odoo.addons.premafirm_ai_engine.models.crm_ai_assistant._gpt',
            fake_gpt)
        return captured, patch_ctx

    def test_draft_request_classification(self):
        import re
        from odoo.addons.premafirm_ai_engine.models import crm_ai_assistant as m
        for text, expected in CLASSIFICATION:
            self.assertEqual(bool(m._DRAFT_REQUEST_RE.search(text)), expected,
                             f'classification mismatch for {text!r}')

    def test_draft_uses_code_prompt_not_legacy_role_prompt(self):
        """Draft requests must NOT receive the editable role prompt (which
        demands Objective/Account insight/Recommended next action sections)."""
        self._post_note('Called Victoria today at 9:30 — voicemail, no '
                        'answer. Follow up tomorrow if nothing back.')
        captured, patch_ctx = self._capture_gpt()
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = EXAMPLE_REQUEST
            self.lead.action_ai_chat_send()
        system = captured['system']
        self.assertIn('INVENT NOTHING', system)
        self.assertIn('OUTPUT FORMAT (email requests)', system)
        self.assertNotIn('RESPONSE FORMAT FOR GENERAL', system)
        # The legacy role prompt's format markers (arrow-prefixed) must be
        # gone; the phrase itself appears only inside rule 16's banned list.
        self.assertNotIn('→ Recommended next action', system)
        self.assertNotIn('→ Account insight', system)
        self.assertIn("Today's actual date is", system)
        self.assertIn('CURRENT DATE', system)
        # Company facts come from the profile block, never the fallback text.
        self.assertIn('=== COMPANY CONTEXT ===', system)
        user_msg = captured['messages'][0]['content']
        self.assertIn('ACCOUNT CONTEXT:', user_msg)
        self.assertIn('REQUEST:', user_msg)

    def test_context_is_dated_and_notes_split_from_emails(self):
        """The model must receive dated sections that separate real emails
        from internal chatter, including the opportunity description."""
        two_weeks = days_ago_utc(14)
        self._post_note('Anna covers purchasing for the account.',
                        dt=two_weeks)
        self._post_note('Called Victoria today — voicemail. Anna out '
                        'today per her out-of-office note.')
        self._post_email('Weekly LTL rates', 'Hi Ahmad, please send '
                         'weekly LTL pricing to Ottawa. Thanks, Victoria',
                         from_partner=self.victoria,
                         dt=days_ago_utc(3))
        captured, patch_ctx = self._capture_gpt()
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = EXAMPLE_REQUEST
            self.lead.action_ai_chat_send()
        ctx = captured['messages'][0]['content']
        # Description + both notes are retrievable, each dated.
        self.assertIn('=== LEAD INTERNAL DESCRIPTION ===', ctx)
        self.assertIn('Weekly LTL candidate', ctx)
        self.assertIn('=== LEAD CHATTER / INTERNAL NOTES', ctx)
        self.assertIn('Anna covers purchasing', ctx)
        self.assertIn('(14 days ago)', ctx)
        self.assertIn('(today)', ctx)
        # The email lives in its own thread section, dated, not today.
        self.assertIn('=== EMAIL THREAD (newest first) ===', ctx)
        self.assertIn('RECEIVED from Victoria Lee', ctx)
        self.assertIn('Weekly LTL rates', ctx)
        self.assertIn('(3 days ago)', ctx)
        # All contacts are listed so the model can address Victoria.
        self.assertIn('Victoria Lee', ctx)

    def test_response_stored_and_composable_body_is_email_only(self):
        self._post_note('Called Victoria today — voicemail. Anna out '
                        'today per her out-of-office note.')
        captured, patch_ctx = self._capture_gpt()
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = EXAMPLE_REQUEST
            self.lead.action_ai_chat_send()
        stored = self.lead.sudo().x_ai_chat_response
        self.assertIn('SUBJECT: Anna is out of the office today', stored)
        # The compose action keeps only the email (no analysis sections).
        action = self.lead.action_ai_compose_email()
        body = action['context']['default_body']
        self.assertIn('Hi Victoria', body)
        self.assertNotIn('Objective', body)
        self.assertNotIn('insight', body)
        self.assertNotIn('recommended', body)

    def test_honest_say_so_when_history_unavailable(self):
        """User asked to check notes but nothing exists: the reply says so
        after a '---' separator and the composed email stays clean."""
        captured, patch_ctx = self._capture_gpt()
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = (
                'Write a brief email to Victoria. Check the internal notes '
                'and the email thread first.')
            self.lead.action_ai_chat_send()
        stored = self.lead.sudo().x_ai_chat_response
        self.assertIn('\n---\nNote for Ahmad', stored)
        self.assertIn('could not retrieve any', stored)
        action = self.lead.action_ai_compose_email()
        self.assertNotIn('Note for Ahmad', action['context']['default_body'])

    def test_no_false_warning_when_history_exists(self):
        self._post_note('Called Victoria today — voicemail.')
        captured, patch_ctx = self._capture_gpt()
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = (
                'Write a brief email to Victoria. Check the internal notes '
                'first.')
            self.lead.action_ai_chat_send()
        stored = self.lead.sudo().x_ai_chat_response
        self.assertNotIn('Note for Ahmad', stored)
        self.assertNotIn('\n---', stored)

    def test_stored_draft_is_email_only_no_signoff(self):
        """The model closes with 'Best regards,\\nAhmad' despite the rules:
        the stored draft (what the chat shows and the composer sends) must end
        at the last content sentence — no sign-off, no name, no postscript."""
        self._post_note('Called Victoria today — voicemail.')
        captured, patch_ctx = self._capture_gpt()  # canned reply ends with a sign-off
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = EXAMPLE_REQUEST
            self.lead.action_ai_chat_send()
        stored = self.lead.sudo().x_ai_chat_response
        self.assertNotIn('Best regards', stored)
        self.assertNotIn('regards', stored)
        # The body ends at the last content sentence (the canned reply's
        # closing paragraph is one long line, so compare via endswith).
        self.assertTrue(stored.rstrip().endswith('when you have a moment.'))
        compose = self.lead.action_ai_compose_email()['context']['default_body']
        self.assertNotIn('regards', compose)
        self.assertIn('voicemail', compose)

    def test_draft_postscript_and_fake_signature_stripped(self):
        """A model reply carrying its own '---' note and a fake 'OdooBot'
        signature is normalized so the stored draft is exactly the email."""
        self._post_note('Anna covers purchasing for this account.')
        answer = (
            'SUBJECT: Re: Weekly LTL rates - Ottawa DC\n\n'
            'Hi Victoria,\n\n'
            'I tried calling you today and reached voicemail.\n\n'
            'Best regards,\nOdooBot\n\n'
            '---\nNote: I reviewed the full history on file.'
        )
        captured, patch_ctx = self._capture_gpt(answer=answer)
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = EXAMPLE_REQUEST
            self.lead.action_ai_chat_send()
        stored = self.lead.sudo().x_ai_chat_response
        self.assertIn('SUBJECT: Re: Weekly LTL rates - Ottawa DC', stored)
        self.assertNotIn('OdooBot', stored)
        self.assertNotIn('---', stored)
        self.assertNotIn('Note:', stored)
        self.assertEqual(stored.rstrip().splitlines()[-1],
                         'I tried calling you today and reached voicemail.')

    def test_signoff_strip_variants(self):
        """Pure-function battery: every common closing is peeled from the end
        of a draft, while closing-flavoured content is never damaged."""
        from odoo.addons.premafirm_ai_engine.models import crm_ai_assistant as m
        content = 'Hi Victoria,\n\nLet me know what works for you.'
        closings = [
            'Best regards,\nOdooBot',
            'Best regards',
            'Kind regards,\nAhmad Ibrahim',
            'Regards,\nAhmad Ibrahim\nOwner/Operator',
            'Best, Ahmad',
            'Thanks,\nAhmad',
            'Thank you,\nAhmad Ibrahim',
            'Many thanks,\nAhmad',
            'With kind regards',
            'Cheers,\nAhmad',
            'Sincerely,\nAhmad Ibrahim',
            'Thanks so much!',
            'Have a great weekend',
            'Yours truly,',
        ]
        for closing in closings:
            stripped = m._strip_trailing_signoff(f'{content}\n\n{closing}')
            self.assertEqual(stripped, content,
                             f'closing {closing!r} not stripped')
        # Closing-flavoured CONTENT must survive untouched.
        survivors = [
            'Thanks for the quick reply.',
            'Thanks for reviewing the quote — I appreciate it.',
            'Talk soon',
            'Thank you for your patience.',
        ]
        for tail in survivors:
            text = f'{content}\n\n{tail}'
            self.assertEqual(m._strip_trailing_signoff(text), text,
                             f'content {tail!r} was damaged')

    def test_general_mode_keeps_role_prompt_plus_discipline(self):
        """Analysis questions keep the editable role prompt but the
        code-side FACT DISCIPLINE block is appended after it."""
        captured, patch_ctx = self._capture_gpt(answer='Direct answer.')
        with patch_ctx:
            self.lead.sudo().x_ai_chat_input = (
                'Why have they not replied? What is the best approach?')
            self.lead.action_ai_chat_send()
        system = captured['system']
        self.assertIn('YOUR ROLE', system)  # role prompt preserved
        self.assertIn('FACT DISCIPLINE (ALWAYS)', system)
        pos_role = system.index('YOUR ROLE')
        pos_fact = system.index('FACT DISCIPLINE (ALWAYS)')
        self.assertGreater(pos_fact, pos_role)  # discipline comes AFTER
        self.assertIn('CURRENT DATE', system)
        self.assertIn("Today's actual date is", system)
