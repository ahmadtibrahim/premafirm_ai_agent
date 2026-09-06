"""Focused safety tests for the quotation confirm / email flow (S00094 guard).

Run targeted only (never the full suite), e.g.:

    odoo-bin -c <conf> -d <staging-db> -u premafirm_ai_engine \\
        --test-tags /quote_confirm_safety --stop-after-init

Scope — the dangerous actions only:
  1. Backend Confirm must NOT email the customer (silent auto-send killed),
     including the S00093 pattern of cancel -> draft -> re-confirm.
  2. The explicit "Send Confirmation Email" action sends exactly once and a
     second attempt is refused (idempotency marker).
  3. A generic ``send_email`` context cannot bypass the staff-only send gate.
  4. AI rate-quote drafts always create DRAFT quotations linked to the source
     CRM lead (opportunity_id), with no email.

Real mail delivery is disabled by patching mail.mail.send — nothing ever leaves
the database or reaches an SMTP/API provider.
"""
import unittest.mock as mock

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.mail.models.mail_mail import MailMail


@tagged('quote_confirm_safety')
class TestQuoteConfirmSafety(TransactionCase):

    def setUp(self):
        super().setUp()
        # No real outbound delivery during tests: mails are created but never sent.
        self.patcher = mock.patch.object(MailMail, 'send', autospec=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.partner = self.env['res.partner'].create({
            'name': 'QCS Test Customer',
            'email': 'qcs-test@example.com',
        })
        self.mail_template = self.env['mail.template'].create({
            'name': 'QCS Load Confirmation',
            'model_id': self.env.ref('sale.model_sale_order').id,
            'email_from': 'PremaFirm <no-reply@example.com>',
            'email_to': '${object.partner_id.email}',
            'subject': 'Load Confirmation ${object.name}',
            'body_html': '<p>Rate confirmation for ${object.partner_id.name}.</p>',
        })
        self.order_template = self.env['sale.order.template'].create({
            'name': 'QCS Load Confirmation - Canada',
            'mail_template_id': self.mail_template.id,
        })

    def _make_order(self):
        """An order carrying the quotation template with its confirmation mail —
        exactly the S00094 configuration (Load Confirmation template + mail)."""
        return self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'sale_order_template_id': self.order_template.id,
        })

    def _mails(self, order):
        return self.env['mail.mail'].search([
            ('mail_message_id.model', '=', 'sale.order'),
            ('mail_message_id.res_id', '=', order.id),
        ])

    def test_backend_confirm_sends_no_email(self):
        """Root cause S00094: backend Confirm must never silently email."""
        order = self._make_order()
        order.action_confirm()
        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.x_confirmation_email_sent_at)
        self.assertEqual(self._mails(order), self.env['mail.mail'])

    def test_cancel_reconfirm_never_emails(self):
        """The S00093 duplicate pattern (cancel -> draft -> re-confirm) must stay silent."""
        order = self._make_order()
        order.action_confirm()
        # On a confirmed order the plain Cancel button opens the cancel wizard;
        # disable_cancel_warning takes the wizard's own path (_action_cancel).
        order.with_context(disable_cancel_warning=True).action_cancel()
        order.action_draft()
        order.action_confirm()
        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.x_confirmation_email_sent_at)
        self.assertEqual(self._mails(order), self.env['mail.mail'])

    def test_explicit_send_is_idempotent(self):
        """The staff 'Send Confirmation Email' action mails exactly once."""
        order = self._make_order()
        order.action_confirm()
        order.action_send_order_confirmation()
        self.assertTrue(order.x_confirmation_email_sent_at)
        self.assertEqual(len(self._mails(order)), 1)
        # Second attempt — same click again, retry, or automation — is refused.
        with self.assertRaises(UserError):
            order.action_send_order_confirmation()
        self.assertEqual(len(self._mails(order)), 1)

    def test_generic_send_email_context_cannot_mail(self):
        """Portal/payment-style context cannot bypass explicit staff sending."""
        order = self._make_order()
        order.with_context(send_email=True).action_confirm()
        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.x_confirmation_email_sent_at)
        self.assertEqual(self._mails(order), self.env['mail.mail'])
        # Staff can still deliberately send the confirmation exactly once.
        order.action_send_order_confirmation()
        self.assertTrue(order.x_confirmation_email_sent_at)
        self.assertEqual(len(self._mails(order)), 1)

    def test_ml_draft_creates_draft_quotation_linked_to_lead(self):
        """AI rate-quote drafts always create a DRAFT quotation linked to the lead."""
        if 'opportunity_id' not in self.env['sale.order']._fields:
            self.skipTest('sale_crm not installed — no opportunity_id on sale.order')
        lead = self.env['crm.lead'].create({
            'name': 'QCS Rate Request - Link Street',
            'partner_id': self.partner.id,
        })
        draft = self.env['premafirm.ml.draft'].create({
            'draft_type': 'rate_quote',
            'ai_suggestion': 'Suggested rate: 1200 CAD',
            'source_model': 'crm.lead',
            'source_id': lead.id,
            'context_snapshot': '{}',
        })
        action = draft.action_create_quotation()
        order = self.env['sale.order'].browse(action['res_id'])
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.opportunity_id.id, lead.id)
        self.assertEqual(self._mails(order), self.env['mail.mail'])
