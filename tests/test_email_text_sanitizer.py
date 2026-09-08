"""
CRM Preliminary Estimate — customer email text sanitizer.

Guarantees under test:
* quoted earlier-thread history (HTML <blockquote> AND plain-text reply
  forms: "On ... wrote:", '>' -prefixed runs) never survives into the text
  that fact extraction reads;
* RFC 3676 signature delimiters ('--') cut the signature and everything
  below it (footer disclaimers included);
* footer boilerplate / "Sent from my iPhone" is stripped from the TAIL;
* FORWARDED messages are the exception: the divider + header block is
  dropped but the forwarded body (the genuine request) is kept;
* the sanitizer never invents or reorders content and keeps non-boilerplate
  text intact.
"""

import re

from odoo.tests import TransactionCase, tagged

from odoo.addons.premafirm_ai_engine.services.email_text_sanitizer import (
    sanitize_email_body_html,
    sanitize_email_text,
)


@tagged("e_a2", "estimate", "email_sanitizer")
class TestEmailTextSanitizer(TransactionCase):
    def _clean(self, text):
        return sanitize_email_text(text)

    # ── HTML bodies (as stored by Odoo on mail.message) ──────────────

    def test_html_blockquote_history_is_cut(self):
        html = (
            "<p>Please quote 3 pallets, 2500 lb, pickup 994 Westport Crescent "
            "Mississauga on Sep 15.</p>"
            "<blockquote>On Fri, Sep 4, 2026 at 10:32 AM John wrote:<br>"
            "We need 22 pallets Toronto to Mascouche as discussed, 21000 lbs "
            "before 4pm.<br></blockquote>"
        )
        text = sanitize_email_body_html(html)
        self.assertIn("3 pallets", text)
        self.assertIn("994 Westport Crescent", text)
        self.assertNotIn("22 pallets", text)      # stale quoted history
        self.assertNotIn("Mascouche", text)
        self.assertNotIn("21000", text)

    def test_html_signature_and_disclaimer_are_removed(self):
        html = (
            "<p>Attached our PO. Delivery before 2:00 PM please.</p>"
            "<p>Thanks,<br>Jane Smith<br>Logistics Coordinator<br>"
            "Acme Distribution<br>jane@acme.example<br>416-555-0134</p>"
            "<p style='font-size:8pt'>This email and any attachments are "
            "confidential and may contain privileged information. If you are "
            "not the intended recipient, please notify the sender. This "
            "message has been scanned for viruses.</p>"
        )
        text = sanitize_email_body_html(html)
        self.assertIn("Delivery before 2:00 PM", text)
        self.assertIn("Jane Smith", text)  # no '--' marker → sig kept as content
        self.assertNotIn("confidential", text)
        self.assertNotIn("privileged", text)
        self.assertNotIn("intended recipient", text)
        self.assertNotIn("viruses", text)

    # ── Plain-text reply quote forms ─────────────────────────────────

    def test_on_wrote_reply_quote_is_cut(self):
        text = (
            "We accept. Pickup Sep 15 between 8 and 9 AM is fine.\n\n"
            "On Mon, Sep 7, 2026 at 9:03 AM John Doe <jdoe@acme.example> "
            "wrote:\n\n"
            "> Please quote: 994 Westport Crescent, Mississauga ON to "
            "211 Bell Boulevard Belleville ON. 3 pallets 2500 lb reefer.\n"
            "> Ref TEST-CRM-001\n"
        )
        clean = self._clean(text)
        self.assertIn("We accept", clean)
        self.assertIn("Sep 15", clean)
        self.assertNotIn("994 Westport", clean)
        self.assertNotIn("TEST-CRM-001", clean)

    def test_gt_prefixed_quote_run_is_cut(self):
        text = (
            "Can you do a delivery on the 15th?\n\n"
            "> From: Jane Smith <jane@acme.example>\n"
            "> Sent: Friday, September 4, 2026 2:12 PM\n"
            "> To: quotes@premafirm.com\n"
            "> Subject: Quote request\n"
            ">\n"
            "> Hi, we need 3 pallets picked up 994 Westport Crescent...\n"
        )
        clean = self._clean(text)
        self.assertIn("delivery on the 15th", clean)
        self.assertNotIn("994 Westport", clean)
        self.assertNotIn("From:", clean)

    def test_signature_delimiter_cuts_signature_and_footer(self):
        text = (
            "Hi, please confirm you received our request.\n\n"
            "-- \n"
            "Jane Smith\n"
            "Acme Distribution\n"
            "This message is confidential and may contain privileged "
            "information.\n"
        )
        clean = self._clean(text)
        self.assertIn("please confirm", clean)
        self.assertNotIn("Jane Smith", clean)
        self.assertNotIn("confidential", clean)

    # ── Forwarded messages keep their content ────────────────────────

    def test_forwarded_message_keeps_the_body(self):
        text = (
            "Please quote this for us.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Supplier X <supply@vendor.example>\n"
            "Date: Wed, Sep 2, 2026 at 8:15 AM\n"
            "Subject: Pickup of 3 pallets Sep 15\n"
            "To: purchasing@acme.example\n"
            "\n"
            "Hi, pickup at 994 Westport Crescent, Mississauga ON L5B 2G2 on "
            "Sep 15 8-9 AM, 3 pallets 2500 lb, reefer 2C, liftgate required "
            "at 211 Bell Boulevard Belleville ON before 2 PM.\n"
        )
        clean = self._clean(text)
        self.assertIn("Please quote this", clean)
        self.assertIn("994 Westport Crescent", clean)
        self.assertIn("liftgate required", clean)
        self.assertNotIn("---------- Forwarded message ---------", clean)
        self.assertNotIn("Subject: Pickup of 3 pallets", clean)

    def test_original_message_divider_cuts_quoted_thread(self):
        text = (
            "Updated: 4 pallets now, not 3.\n\n"
            "-----Original Message-----\n"
            "From: Jane <jane@acme.example>\n"
            "Sent: Sep 4, 2026 10:00 AM\n"
            "To: quotes@premafirm.com\n"
            "Subject: 3 pallets request\n"
            "\n"
            "3 pallets 2500 lb pickup Sep 15...\n"
        )
        clean = self._clean(text)
        self.assertIn("4 pallets now", clean)
        self.assertNotIn("Original Message", clean)
        self.assertNotIn("2500", clean)  # stale thread history below the cut

    def test_pure_quote_or_reply_history_returns_empty(self):
        # A reply that only contains the quoted history has no live content.
        text = (
            "On Mon, Sep 7, 2026 at 9:03 AM John Doe wrote:\n"
            "> Please quote 994 Westport Crescent...\n"
        )
        self.assertEqual(self._clean(text), "")

    # ── Tail boilerplate / device sign-offs ──────────────────────────

    def test_tail_footer_and_sent_from_are_stripped(self):
        text = (
            "Please arrange for a liftgate.\n"
            "Thanks,\n"
            "Jane\n"
            "Sent from my iPhone\n"
        )
        clean = self._clean(text)
        self.assertIn("liftgate", clean)
        self.assertNotIn("Sent from my iPhone", clean)

    # ── Content preservation ─────────────────────────────────────────

    def test_ref_and_content_lines_survive(self):
        text = (
            "Ref: TEST-CRM-001\n"
            "To: quotes@premafirm.com\n"
            "3 pallets, 2500 lb, reefer 2C.\n"
        )
        clean = self._clean(text)
        self.assertIn("Ref: TEST-CRM-001", clean)   # never treated as a header
        self.assertIn("3 pallets", clean)

    def test_divider_without_headers_is_kept(self):
        text = "Commodity: retail supplies\n----------------\nWeight 2500 lb\n"
        clean = self._clean(text)
        self.assertIn("retail supplies", clean)
        self.assertIn("2500", clean)

    def test_boilerplate_word_inside_content_is_kept(self):
        # A footer pattern only strips from the TAIL — never inside content.
        text = (
            "We treat all pricing as confidential between us. 3 pallets "
            "2500 lb.\n\n"
            "-- \n"
            "Jane\n"
        )
        clean = self._clean(text)
        self.assertIn("confidential between us", clean)
        self.assertIn("3 pallets", clean)

    def test_single_marker_inside_content_is_never_eaten(self):
        # A lone strong/weak marker mid-sentence is content, not a footer.
        text = (
            "Please ensure the driver is not directed for delivery until "
            "Sep 15; 3 pallets.\n"
        )
        clean = self._clean(text)
        self.assertIn("not directed for delivery", clean)
        self.assertIn("3 pallets", clean)

    def test_multi_marker_footer_block_is_removed(self):
        text = (
            "Attached is our PO; delivery before 2 PM.\n"
            "Thanks.\n"
            "\n"
            "This message contains confidential and privileged information.\n"
            "If you are not the intended recipient please delete it.\n"
        )
        clean = self._clean(text)
        self.assertIn("PO; delivery before 2 PM", clean)
        self.assertNotIn("confidential", clean)
        self.assertNotIn("privileged", clean)
        self.assertNotIn("intended recipient", clean)
