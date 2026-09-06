"""
E-A2 — Preliminary Estimate Reply draft (master instruction §3.3).

Guarantees under test:
* preparing a draft NEVER sends mail (MailMail.send is patched and asserted
  never-called; no mail.mail row is ever created);
* the price shown comes ONLY from the caller's parameter and is injected
  programmatically — the LLM never writes price text;
* a concise NON-BINDING disclaimer is always present;
* every draft is discoverable on its opportunity (x_estimate_reply_ids) and
  carries the visible-sourcing facts snapshot (email-sourced times stay
  sourced to the customer email, description facts to the description);
* omitting the price raises — the engine never invents prices.

The fixture mirrors Lead 1041: the description states the pallet count, and a
NEWER customer email states the corrected pickup window (10:30-11:30 a.m.) and
delivery deadline (before 4:00 p.m., Tue Sep 8 2026).
"""

from contextlib import ExitStack
from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

PROSE = (
    "Thank you for your request.\n\n"
    "We confirm receipt of the shipment details and are preparing the formal "
    "Customer Rate Confirmation, which we will send to you shortly."
)


def _fact(field, value):
    return {"field": field, "value": value, "confidence": "high",
            "conflict": False}


def _fake_extractor():
    """Deterministic stand-in for the AI extractor: rows only for tokens
    actually present, each row carrying that document's own provenance."""
    def extractor(text, source="", kind="lead_description", at=None):
        rows = []
        matches = [
            ("22 pallets", "pallets", "22"),
            ("September 8 2026", "pickup_date", "2026-09-08"),
            ("10:30 a.m.", "pickup_earliest", "10:30"),
            ("11:30 a.m.", "pickup_latest", "11:30"),
            ("before 4:00 p.m.", "delivery_deadline", "16:00"),
        ]
        for token, field, value in matches:
            if token in text:
                rows.append(dict(_fact(field, value), source=source,
                                 kind=kind, at=at))
        return {"rows": rows, "warnings": []}
    return extractor


def _patches(**overrides):
    """Return a context manager patching the deepseek_utils entry points
    (patches live until the with-block exits).  The prose mock returns text
    only when an API key is present — mirroring the real client, which cannot
    answer without a key, so the no-key path exercises the fallback."""
    base = {
        "get_api_key": lambda env: "test-key",
        "get_model": lambda env: "test-model",
        "deepseek_chat": lambda *a, **k: PROSE if k.get("api_key") else "",
    }
    base.update(overrides)
    stack = ExitStack()
    for key, value in base.items():
        stack.enter_context(
            patch("odoo.addons.premafirm_ai_engine.services.deepseek_utils.%s" % key,
                  value))
    return stack


@tagged("e_a2", "estimate_reply_draft")
class TestEstimateReplyDraft(TransactionCase):
    def setUp(self):
        super().setUp()
        self.lead = self.env["crm.lead"].create({
            "name": "Lead 1041 — 22 pallets Toronto to Mascouche",
            "description": ("<p>22 pallets of retail store supplies, Toronto "
                            "to Mascouche.</p>")})
        customer = self.env["res.partner"].create({
            "name": "Acme Customer", "email": "customer@acme.example"})
        from datetime import datetime, timedelta
        self.env["mail.message"].sudo().create({
            "model": "crm.lead",
            "res_id": self.lead.id,
            "message_type": "email",
            "subject": "Correction — pickup window and delivery",
            "body": ("<p>Pickup Tuesday September 8 2026, 10:30 a.m. to "
                     "11:30 a.m., delivery before 4:00 p.m. Thanks.</p>"),
            "author_id": customer.id,
            "date": datetime.utcnow() + timedelta(hours=1),
        })
        self.send = Mock()
        self.mail_mail = __import__(
            "odoo.addons.mail.models.mail_mail", fromlist=["MailMail"]).MailMail

    def _prepare(self, price=1234.5, reference="RC-1041 via dispatch PricingService",
                 **kw):
        return self.env["premafirm.lead.estimate.reply"].prepare_from_lead(
            self.lead, price_amount=price, price_reference=reference,
            extractor=_fake_extractor(), **kw)

    def test_prepare_creates_discoverable_draft_and_sends_nothing(self):
        from odoo.tools import html2plaintext
        before = self.env["mail.mail"].search_count([])
        with patch.object(self.mail_mail, "send", autospec=True) as send, \
                _patches():
            draft = self._prepare()
        self.assertTrue(draft.id)
        # Discoverable on the opportunity (engine-local link surface).
        self.assertIn(draft, self.lead.x_estimate_reply_ids)
        self.assertEqual(draft.crm_lead_id, self.lead)
        self.assertEqual(draft.partner_id, self.lead.partner_id)
        # Price came from the caller and is recorded.
        self.assertEqual(draft.price_amount, 1234.5)
        self.assertEqual(draft.price_reference, "RC-1041 via dispatch PricingService")
        # The draft body shows the price line and the non-binding disclaimer.
        body = html2plaintext(draft.body_html or "")
        self.assertIn("Estimated price", body)
        self.assertIn("1,234.50", body)
        self.assertIn("NON-BINDING", body)
        self.assertIn("nothing here has been sent", body.lower())
        self.assertTrue(draft.subject)
        # NO mail.mail row was created and the send method was never invoked.
        self.assertEqual(self.env["mail.mail"].search_count([]), before)
        send.assert_not_called()

    def test_prose_never_carries_the_price_or_disclaimer(self):
        """AI prose must not contain price/disclaimer text — those are added
        programmatically exactly once."""
        from odoo.tools import html2plaintext
        with patch.object(self.mail_mail, "send", autospec=True), _patches():
            draft = self._prepare()
        body = html2plaintext(draft.body_html or "")
        self.assertEqual(body.count("Estimated price"), 1)
        self.assertEqual(body.count("NON-BINDING"), 1)
        # AI prose stayed clean (note empty = full AI path succeeded).
        self.assertFalse(draft.generation_note)

    def test_no_api_key_falls_back_without_dying(self):
        from odoo.tools import html2plaintext
        before = self.env["mail.mail"].search_count([])
        with patch.object(self.mail_mail, "send", autospec=True), \
                _patches(get_api_key=lambda env: None):
            draft = self._prepare()
        self.assertTrue(draft.id)
        body = html2plaintext(draft.body_html or "")
        self.assertIn("Estimated price", body)  # programmatic price still there
        self.assertIn("NON-BINDING", body)
        self.assertIn("fallback", (draft.generation_note or "").lower())
        self.assertEqual(self.env["mail.mail"].search_count([]), before)

    def test_price_is_required_never_invented(self):
        with patch.object(self.mail_mail, "send", autospec=True), _patches():
            with self.assertRaises(UserError):
                self._prepare(price=None)

    def test_facts_snapshot_keeps_visible_sourcing(self):
        """Email-stated facts stay sourced to the customer email, description
        facts to the description — supersession did not erase provenance."""
        with patch.object(self.mail_mail, "send", autospec=True), _patches():
            draft = self._prepare()
        snap = draft.facts_snapshot or {}
        eff = snap.get("effective") or {}
        self.assertEqual(eff["pickup_earliest"]["value"], "10:30")
        self.assertIn("Customer email", eff["pickup_earliest"]["source"])
        self.assertEqual(eff["pickup_earliest"]["kind"], "inbound_email")
        self.assertEqual(eff["delivery_deadline"]["value"], "16:00")
        self.assertEqual(eff["pickup_date"]["value"], "2026-09-08")
        self.assertEqual(eff["pallets"]["value"], "22")
        self.assertEqual(eff["pallets"]["kind"], "lead_description")
        # The generated body names the shipment facts with their source.
        from odoo.tools import html2plaintext
        body = html2plaintext(draft.body_html or "")
        self.assertIn("10:30", body)
        self.assertIn("Pallets: 22", body)

    def test_model_has_no_send_api(self):
        """The draft model is a draft by construction: no send action exists."""
        model = self.env["premafirm.lead.estimate.reply"]
        methods = {m for m in dir(model) if m.startswith("action_")
                   or m.startswith("send")}
        self.assertFalse(methods & {"action_send", "send", "action_send_rc"})
