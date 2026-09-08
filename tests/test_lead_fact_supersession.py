"""
E-A2 — shipment-fact supersession ordering (master instruction §3.2).

Covers the "Lead 1041" semantics on pure functions (absolute datetimes, no
DB) and the document-gathering pipeline on a real lead (customer emails vs
lead description vs staff-authored chatter).
"""

from datetime import datetime, timedelta

from odoo.tests import TransactionCase, tagged


def _fact(field, value, source, kind, at, confidence="high"):
    return {"field": field, "value": value, "source": source, "kind": kind,
            "at": at, "confidence": confidence}


@tagged("e_a2", "lead_fact_supersession")
class TestSupersessionOrdering(TransactionCase):
    """Pure resolve_effective_facts ordering — Lead-1041 fixture."""

    def _fixture_candidates(self):
        """Lead 1041: original description gives 09:00-10:00 pickup and a 13:00
        delivery deadline; the NEWEST customer email corrects pickup to
        10:30-11:30 a.m. and delivery to before 4:00 p.m. (both Tue Sep 8
        2026)."""
        lead_doc = _fact("pickup_earliest", "09:00", "Lead description",
                         "lead_description", datetime(2026, 9, 2, 9, 0))
        lead_late = _fact("pickup_latest", "10:00", "Lead description",
                          "lead_description", datetime(2026, 9, 2, 9, 0))
        lead_dead = _fact("delivery_deadline", "13:00", "Lead description",
                          "lead_description", datetime(2026, 9, 2, 9, 0))
        lead_date = _fact("pickup_date", "2026-09-08", "Lead description",
                          "lead_description", datetime(2026, 9, 2, 9, 0))
        corrected = [
            _fact("pickup_earliest", "10:30", "Customer email 2026-09-05 14:30",
                  "inbound_email", datetime(2026, 9, 5, 14, 30)),
            _fact("pickup_latest", "11:30", "Customer email 2026-09-05 14:30",
                  "inbound_email", datetime(2026, 9, 5, 14, 30)),
            _fact("delivery_deadline", "16:00", "Customer email 2026-09-05 14:30",
                  "inbound_email", datetime(2026, 9, 5, 14, 30)),
        ]
        return [lead_doc, lead_late, lead_dead, lead_date] + corrected

    def test_newer_customer_corrections_supersede_older_details(self):
        result = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["resolve_effective_facts"]).resolve_effective_facts(
                self._fixture_candidates())
        eff = result["effective"]
        # Newest explicit customer statements win...
        self.assertEqual(eff["pickup_earliest"]["value"], "10:30")
        self.assertEqual(eff["pickup_latest"]["value"], "11:30")
        self.assertEqual(eff["delivery_deadline"]["value"], "16:00")
        self.assertEqual(eff["pickup_date"]["value"], "2026-09-08")
        # ...and stay visibly sourced.
        self.assertIn("Customer email 2026-09-05", eff["pickup_earliest"]["source"])
        self.assertIn("Customer email 2026-09-05", eff["delivery_deadline"]["source"])
        # The older statements are reported as superseded, not silently lost.
        superseded_values = sorted(f["value"] for f in result["superseded"])
        self.assertIn("09:00", superseded_values)
        self.assertIn("10:00", superseded_values)
        self.assertIn("13:00", superseded_values)
        # Every superseded fact keeps its provenance too.
        self.assertTrue(all(f["source"] for f in result["superseded"]))

    def test_empty_value_never_supersedes_a_concrete_fact(self):
        """A later email saying the pickup time is 'unknown' must not erase
        the concrete corrected window."""
        resolve = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["resolve_effective_facts"]).resolve_effective_facts
        candidates = self._fixture_candidates() + [
            _fact("pickup_earliest", "unknown", "Customer email 2026-09-06 08:00",
                  "inbound_email", datetime(2026, 9, 6, 8, 0)),
            _fact("pickup_latest", "n/a", "Customer email 2026-09-06 08:00",
                  "inbound_email", datetime(2026, 9, 6, 8, 0)),
        ]
        eff = resolve(candidates)["effective"]
        self.assertEqual(eff["pickup_earliest"]["value"], "10:30")
        self.assertEqual(eff["pickup_latest"]["value"], "11:30")

    def test_same_timestamp_source_kind_breaks_the_tie(self):
        """Attachment vs email on the same document time: the explicit
        customer email wins; the lead description loses to both."""
        resolve = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["resolve_effective_facts"]).resolve_effective_facts
        at = datetime(2026, 9, 4, 10, 0)
        candidates = [
            _fact("pallets", "20", "Lead description", "lead_description", at),
            _fact("pallets", "22", "Rate sheet attachment", "attachment", at),
            _fact("pallets", "24", "Customer email 2026-09-04", "inbound_email", at),
        ]
        eff = resolve(candidates)["effective"]
        self.assertEqual(eff["pallets"]["value"], "24")
        self.assertEqual(len(resolve(candidates)["superseded"]), 2)

    def test_duplicate_candidate_is_not_superseded(self):
        resolve = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["resolve_effective_facts"]).resolve_effective_facts
        at = datetime(2026, 9, 4, 10, 0)
        dup = _fact("pallets", "22", "Same email", "inbound_email", at)
        result = resolve([dup, dict(dup)])
        self.assertEqual(result["effective"]["pallets"]["value"], "22")
        self.assertEqual(result["superseded"], [])

    def test_unknown_fields_and_empty_values_are_ignored(self):
        resolve = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["resolve_effective_facts"]).resolve_effective_facts
        at = datetime(2026, 9, 4, 10, 0)
        candidates = [
            _fact("", "x", "doc", "attachment", at),
            _fact("price", "never extracted", "doc", "attachment", at),
            _fact("pallets", "", "doc", "attachment", at),
            _fact("pallets", None, "doc", "attachment", at),
        ]
        result = resolve(candidates)
        self.assertEqual(result["effective"], {})
        self.assertEqual(result["superseded"], [])


@tagged("e_a2", "lead_fact_supersession")
class TestLeadFactServicePipeline(TransactionCase):
    """collect_documents + extract_effective_facts over a real lead."""

    def setUp(self):
        super().setUp()
        self.lead = self.env["crm.lead"].create({
            "name": "Lead 1041 fixture",
            "description": (
                "<p>Pickup Tue Sep 8 2026 between 09:00 and 10:00, delivery "
                "by 13:00. Pallets 20.</p>"),
        })
        self.customer = self.env["res.partner"].create({
            "name": "Acme Customer", "email": "customer@acme.example"})
        now = datetime.utcnow()

        def _email(delta_hours, body, author):
            return self.env["mail.message"].sudo().create({
                "model": "crm.lead",
                "res_id": self.lead.id,
                "message_type": "email",
                "subject": "Shipment details",
                "body": body,
                "author_id": author.id,
                "date": now + timedelta(hours=delta_hours),
            })

        # Older customer email — the first correction (day +1).
        _email(26, "<p>Correction: pickup window is now 10:30 to 11:30 a.m. "
                   "and delivery must be before 4:00 p.m. Thanks.</p>",
               self.customer)
        # Newest customer email (day +2) — later correction, explicit.
        _email(50, "<p>To confirm: pickup 10:30-11:30 a.m. Tuesday, delivery "
                   "BEFORE 4:00 p.m. Commodity is retail store supplies.</p>",
               self.customer)
        # Staff-authored email must NEVER count as a shipment-fact source.
        _email(74, "<p>Internal guess at times...</p>", self.env.user.partner_id)

    def _fake_extractor(self):
        """Deterministic stand-in for the AI extractor: returns rows only for
        tokens actually present in the document text (never invents)."""
        def extractor(text, source_label=None, kind="lead_description",
                      at=None):
            rows = []
            if "09:00 and 10:00" in text:
                rows.append(_fact("pickup_earliest", "09:00", source_label,
                                  kind, at))
                rows.append(_fact("pickup_latest", "10:00", source_label,
                                  kind, at))
                rows.append(_fact("pallets", "20", source_label, kind, at))
            if "13:00" in text and "09:00 and 10:00" in text:
                rows.append(_fact("delivery_deadline", "13:00", source_label,
                                  kind, at))
            if "10:30 to 11:30" in text:
                rows.append(_fact("pickup_earliest", "10:30", source_label,
                                  kind, at))
                rows.append(_fact("pickup_latest", "11:30", source_label,
                                  kind, at))
            if "before 4:00 p.m." in text:
                rows.append(_fact("delivery_deadline", "16:00", source_label,
                                  kind, at))
            if "retail store supplies" in text:
                rows.append(_fact("commodity", "Retail store supplies",
                                  source_label, kind, at))
            return {"rows": rows, "warnings": []}
        return extractor

    def test_document_gathering_excludes_staff_and_orders_by_date(self):
        svc = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["LeadFactService"]).LeadFactService(self.env)
        docs = svc.collect_documents(self.lead)
        # lead description + 2 customer emails; the staff-authored email is
        # excluded, and documents come oldest -> newest.
        self.assertEqual([d["kind"] for d in docs],
                         ["lead_description", "inbound_email", "inbound_email"])
        self.assertEqual(docs[0]["source"], "Lead description")
        for doc in docs[1:]:
            self.assertIn("Customer email", doc["source"])

    def test_email_quoted_history_and_signature_never_reach_documents(self):
        """A customer email that replies over the quoted earlier thread + a
        '--' signature must contribute ONLY its live text to the documents
        — stale quoted statements (another shipment's pallet count, an old
        reference) must not become candidate facts."""
        _email = lambda body: self.env["mail.message"].sudo().create({  # noqa: E731
            "model": "crm.lead",
            "res_id": self.lead.id,
            "message_type": "email",
            "subject": "Re: Shipment details",
            "body": body,
            "author_id": self.customer.id,
            "date": datetime.utcnow() + timedelta(hours=98),
        })
        _email(
            "<p>Now 6 pallets instead of 3, delivery still before 4:00 p.m. "
            "on Tuesday.</p>"
            "<blockquote>"
            "<p>On Fri, Sep 4, 2026 at 10:32 AM Acme wrote: please quote 55 "
            "pallets dry van to Mascouche ref 2026-OLD-88.</p>"
            "</blockquote>"
            "<p>-- <br>Jane Smith<br>Acme Distribution</p>"
            "<p>This email is confidential and privileged. If you are not "
            "the intended recipient please delete it.</p>"
        )
        svc = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["LeadFactService"]).LeadFactService(self.env)
        docs = svc.collect_documents(self.lead)
        newest = docs[-1]  # kind inbound_email, the email just created
        self.assertEqual(newest["kind"], "inbound_email")
        self.assertIn("6 pallets", newest["text"])
        self.assertNotIn("55 pallets", newest["text"])    # quoted history
        self.assertNotIn("Mascouche", newest["text"])
        self.assertNotIn("2026-OLD-88", newest["text"])   # stale reference
        self.assertNotIn("Jane Smith", newest["text"])    # '--' signature cut
        self.assertNotIn("confidential", newest["text"])  # footer cut
        self.assertNotIn("intended recipient", newest["text"])
        # And no OTHER document picked the quoted junk up either.
        all_text = "\n".join(d["text"] for d in docs)
        self.assertNotIn("55 pallets", all_text)

    def test_lead_1041_pipeline_effective_facts(self):
        """End-to-end: newest customer correction wins and stays sourced."""
        svc = __import__(
            "odoo.addons.premafirm_ai_engine.services.lead_fact_service",
            fromlist=["LeadFactService"]).LeadFactService(self.env)
        result = svc.extract_effective_facts(self.lead,
                                             extractor=self._fake_extractor())
        eff = result["effective"]
        self.assertEqual(eff["pickup_earliest"]["value"], "10:30")
        self.assertEqual(eff["pickup_latest"]["value"], "11:30")
        self.assertEqual(eff["delivery_deadline"]["value"], "16:00")
        self.assertEqual(eff["commodity"]["value"], "Retail store supplies")
        # Provenance is visible on every effective fact (email source, newer).
        for field in ("pickup_earliest", "pickup_latest", "delivery_deadline",
                      "commodity"):
            self.assertIn("Customer email", eff[field]["source"])
            self.assertEqual(eff[field]["kind"], "inbound_email")
        self.assertEqual(eff["pallets"]["value"], "20")
        self.assertEqual(eff["pallets"]["kind"], "lead_description")
        # The old window facts were superseded and reported.
        superseded = [f["value"] for f in result["superseded"]]
        self.assertIn("09:00", superseded)
        self.assertIn("13:00", superseded)
        # No document went missing, no warnings from the fake extractor.
        self.assertEqual(len(result["docs"]), 3)
        self.assertEqual(result["warnings"], [])
