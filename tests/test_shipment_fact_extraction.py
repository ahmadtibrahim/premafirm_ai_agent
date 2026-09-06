"""
E-A2 — structured shipment-fact extraction (master instruction §5).

Covers: text extraction rows carrying source/kind/at/confidence; per-field
conflict flagging (surfaced for review); vocabulary sanitization (off-vocab
fields and empty values never become facts); file + pasted-text extraction
through the shared document extractor; and the hard guarantee that extraction
has ZERO side effects (no mail, no booking/quote creation).
"""

import base64
import json
from contextlib import ExitStack
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

CLEAN_ROWS = json.dumps([
    {"field": "pickup_date", "value": "2026-09-08", "confidence": "high",
     "note": "stated"},
    {"field": "pickup_earliest", "value": "10:30", "confidence": "high",
     "note": "quoted"},
    {"field": "delivery_deadline", "value": "16:00", "confidence": "high",
     "note": "quoted"},
    {"field": "pallets", "value": "22", "confidence": "high", "note": ""},
    {"field": "weight_lbs", "value": "21000", "confidence": "high", "note": ""},
])

CONFLICT_ROWS = json.dumps([
    {"field": "delivery_deadline", "value": "16:00", "confidence": "high",
     "note": "quoted"},
    {"field": "delivery_deadline", "value": "15:00", "confidence": "medium",
     "note": "later sentence"},
    {"field": "pallets", "value": "22", "confidence": "high", "note": ""},
    {"field": "pallets", "value": "", "confidence": "low", "note": "blank"},
    {"field": "price", "value": "899", "confidence": "high",
     "note": "must be dropped — not a shipment fact"},
    {"field": "weight_lbs", "value": "21000", "confidence": "high", "note": ""},
    {"field": "pickup_date", "value": "2026-09-08", "confidence": "high",
     "note": "stated"},
    {"field": "pickup_earliest", "value": "10:30", "confidence": "high",
     "note": "quoted"},
])


def _patch_ai(**overrides):
    """Return a context manager patching the deepseek_utils entry points
    the service imports per call (patches live until the with-block exits)."""
    base = {
        "get_api_key": lambda env: "test-key",
        "get_model": lambda env: "test-model",
        "deepseek_chat": lambda *a, **k: CLEAN_ROWS,
    }
    base.update(overrides)
    stack = ExitStack()
    for key, value in base.items():
        stack.enter_context(
            patch("odoo.addons.premafirm_ai_engine.services.deepseek_utils.%s" % key,
                  value))
    return stack


@tagged("e_a2", "shipment_fact_extraction")
class TestShipmentFactExtractionService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.svc = __import__(
            "odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service",  # noqa: E501
            fromlist=["ShipmentFactExtractionService"]).ShipmentFactExtractionService(self.env)

    def test_extract_rows_carry_source_kind_at_and_confidence(self):
        with _patch_ai():
            result = self.svc.extract_from_text(
                "Pickup Sep 8, pickup 10:30 AM, delivery before 4pm, 22 pallets, 21000 lbs.",
                source_label="Customer email 2026-09-05 14:30",
                kind="inbound_email", at="2026-09-05 14:30:00")
        rows = {r["field"]: r for r in result["rows"]}
        self.assertEqual(rows["pickup_date"]["value"], "2026-09-08")
        self.assertEqual(rows["pickup_earliest"]["value"], "10:30")
        self.assertEqual(rows["delivery_deadline"]["value"], "16:00")
        self.assertEqual(rows["pallets"]["value"], "22")
        self.assertEqual(rows["weight_lbs"]["value"], "21000")
        for r in result["rows"]:
            self.assertEqual(r["source"], "Customer email 2026-09-05 14:30")
            self.assertEqual(r["kind"], "inbound_email")
            self.assertEqual(r["at"], "2026-09-05 14:30:00")
            self.assertIn(r["confidence"], ("high", "medium", "low"))

    def test_conflicting_statements_are_flagged_not_resolved(self):
        with _patch_ai(deepseek_chat=lambda *a, **k: CONFLICT_ROWS):
            result = self.svc.extract_from_text(
                "delivery before 4pm ... actually before 3pm",
                source_label="Customer email", kind="inbound_email", at=None)
        deadline_rows = [r for r in result["rows"]
                         if r["field"] == "delivery_deadline"]
        self.assertEqual(len(deadline_rows), 2)
        # Both are flagged as conflicting; the service NEVER auto-resolves.
        self.assertTrue(all(r["conflict"] for r in deadline_rows))
        non_conflict = [r for r in result["rows"] if r["field"] != "delivery_deadline"]
        self.assertTrue(all(not r["conflict"] for r in non_conflict))

    def test_vocabulary_sanitization_drops_non_facts_with_warning(self):
        with _patch_ai(deepseek_chat=lambda *a, **k: CONFLICT_ROWS):
            result = self.svc.extract_from_text(
                "22 pallets for pickup", source_label="doc", kind="attachment", at=None)
        fields = [r["field"] for r in result["rows"]]
        # 'price' is not in the shared vocabulary and never becomes a fact.
        self.assertNotIn("price", fields)
        # The empty pallets value was dropped, the concrete one kept.
        self.assertEqual([r["value"] for r in result["rows"]
                          if r["field"] == "pallets"], ["22"])
        self.assertTrue(any("not in shared vocabulary" in w for w in result["warnings"]))

    def test_extraction_never_creates_mail_booking_or_quote(self):
        before_mail = self.env["mail.mail"].search_count([])
        logistics = None
        if "logistics.custom.quote" in self.env.registry:
            logistics = self.env["logistics.custom.quote"].search_count([])
        if "logistics.booking" in self.env.registry:
            before_booking = self.env["logistics.booking"].search_count([])
        with _patch_ai():
            self.svc.extract_from_text("22 pallets", source_label="doc",
                                       kind="attachment", at=None)
        self.assertEqual(self.env["mail.mail"].search_count([]), before_mail)
        if logistics is not None:
            self.assertEqual(
                self.env["logistics.custom.quote"].search_count([]), logistics)
        if "logistics.booking" in self.env.registry:
            self.assertEqual(
                self.env["logistics.booking"].search_count([]), before_booking)

    def test_pasted_text_file_extraction_goes_through_document_extractor(self):
        """Pasted text arrives as base64 with no filename — the document
        extractor reads it as plain text, then rows are extracted with
        kind='attachment'."""
        payload = base64.b64encode(
            b"Pickup Tuesday Sep 8 2026, 22 pallets of retail store supplies, "
            b"delivery before 4:00 p.m. thanks").decode()
        with _patch_ai():
            result = self.svc.extract_from_attachment(
                payload, mimetype="text/plain", filename="pasted.txt",
                source_label="Customer paste")
        self.assertEqual(result["method"], "plain")
        self.assertEqual(result["rows"][0]["kind"], "attachment")
        self.assertTrue(all(r["source"] == "Customer paste" for r in result["rows"]))

    def test_undecodable_file_reports_warning_without_crash(self):
        result = self.svc.extract_from_attachment(
            "bm90LWEtcmVhbC1maWxl", mimetype="application/x-unknown",
            filename="weird.dat", source_label="Weird file")
        self.assertEqual(result["rows"], [])
        self.assertTrue(any("Could not read any text" in w for w in result["warnings"]))

    def test_no_api_key_skips_extraction_with_warning(self):
        with _patch_ai(get_api_key=lambda env: None):
            result = self.svc.extract_from_text("22 pallets", source_label="d",
                                                kind="attachment", at=None)
        self.assertEqual(result["rows"], [])
        self.assertTrue(any("API key" in w for w in result["warnings"]))


@tagged("e_a2", "shipment_fact_extraction")
class TestFactMergeHelpers(TransactionCase):
    """Pure helpers: merge_rows dedupes exact duplicates, flag_conflicts only
    flags distinct values of the same field."""

    def test_merge_and_conflict_helpers(self):
        mod = __import__(
            "odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service",  # noqa: E501
            fromlist=["merge_rows", "flag_conflicts", "sanitize_rows"])
        row = {"field": "pallets", "value": "22", "source": "e", "kind": "a",
               "at": None, "confidence": "high"}
        merged = mod.merge_rows([row, dict(row)])
        self.assertEqual(len(merged), 1)
        flagged = mod.flag_conflicts([
            {"field": "pallets", "value": "22"},
            {"field": "pallets", "value": "24"},
            {"field": "commodity", "value": "Dry goods"},
        ])
        by_value = {f["value"]: f["conflict"] for f in flagged}
        self.assertTrue(by_value["22"] and by_value["24"])
        rows, warnings = mod.sanitize_rows([
            {"field": "PALLETS", "value": "22", "confidence": "HIGH"},
            {"field": "not_a_field", "value": "x", "confidence": "high"},
            {"field": "pallets", "value": "", "confidence": "low"},
            {"field": "delivery_deadline", "value": "16:00", "confidence": "ok"},
        ], "src", "attachment", None)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("not in shared vocabulary" in w for w in warnings))
