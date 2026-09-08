"""Estimator structured-stop status (2026-09-08 regression).

Street-less loadboard postings ("Pick up at Cobourg, ON K9A 4R5" — no
street published, e.g. Baxter's Bakery Ref# 3042042) are resolved by the
estimate flow's geocode pass to a postal area / city centre and then
routed and priced. The status badge must reflect ROUTING readiness, not
facility-savability: a row with resolved coordinates is never
"Incomplete Address — Not Saved". Regression: real runs priced the lane
while both stops wore the red incomplete badge (coords present, address
empty, lat/lng not chained into the compute).
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

STOP_MODEL = "premafirm.estimator.structured.stop"


@tagged("structured_stop")
class TestEstimatorStructuredStopStatus(TransactionCase):
    """Street-less rows: incomplete only while NOT yet located."""

    def setUp(self):
        super().setUp()
        brand = self.env["fleet.vehicle.model.brand"].create(
            {"name": "Status test brand"})
        model = self.env["fleet.vehicle.model"].create(
            {"name": "Status test model", "brand_id": brand.id})
        vehicle = self.env["fleet.vehicle"].create(
            {"name": "Status test truck", "model_id": model.id})
        self.request = self.env["premafirm.estimator.scenario.request"].create({
            "vehicle_id": vehicle.id,
        })

    def _stop(self, **kw):
        vals = {"request_id": self.request.id, "stop_type": "pickup"}
        vals.update(kw)
        return self.env[STOP_MODEL].create(vals)

    def test_street_less_without_coordinates_is_incomplete(self):
        """A street-less city+postal row not yet geocoded is still the
        'needs locating' nudge — nothing has resolved it yet."""
        stop = self._stop(city="Cobourg", province="ON",
                          postal_code="K9A 4R5")
        self.assertEqual(stop.status, "incomplete")

    def test_street_less_with_resolved_coordinates_is_manual(self):
        """The canonical regression: geocoding fills lat/lng on the
        street-less stop → routable → never the red incomplete badge."""
        stop = self._stop(city="Cobourg", province="ON",
                          postal_code="K9A 4R5")
        stop.write({"lat": 43.9592, "lng": -78.1631})
        self.assertEqual(stop.status, "manual")
        self.assertEqual(stop._stop_dict()["status"], "manual")

    def test_street_with_locator_is_manual_even_before_geocoding(self):
        """Unchanged behavior: full identity (street + city/postal) is
        manual regardless of coordinates — the run geocodes it."""
        stop = self._stop(address="115 Chambers Dr", city="Ajax",
                          province="ON", postal_code="L1Z 1E4")
        self.assertEqual(stop.status, "manual")

    def test_address_without_locator_is_incomplete(self):
        stop = self._stop(address="115 Chambers Dr")
        self.assertEqual(stop.status, "incomplete")

    def test_empty_row_is_incomplete(self):
        stop = self._stop()
        self.assertEqual(stop.status, "incomplete")

    def test_modified_poke_heals_stale_badge(self):
        """Rows persisted under the OLD compute rule (street-less rows kept
        the red 'incomplete' badge after geocoding) carry NO recompute todo
        — coords were written behind the ORM's back by the pre-fix code
        path. The flow's poke (modified + flush) must recompute and store
        the corrected status for such rows."""
        stop = self._stop(city="Cobourg", province="ON",
                          postal_code="K9A 4R5")
        # Simulate a pre-fix row: coords present in the DB, badge stale.
        self.env.cr.execute(
            "UPDATE premafirm_estimator_structured_stop SET lat=43.9592, "
            "lng=-78.1631, status='incomplete' WHERE id=%s", (stop.id,))
        self.env.invalidate_all()
        self.assertEqual(stop.status, "incomplete")  # genuinely stale
        stop.modified(["lat", "lng", "postal_code", "address", "city",
                       "province"])
        stop.flush_recordset()
        self.env.invalidate_all()
        self.assertEqual(stop.status, "manual")

    def test_saved_location_statuses(self):
        """Reuse/pending-review labels (dispatch module present only)."""
        if "prema.dispatch.location" not in self.env.registry:
            self.skipTest("prema_dispatch is not installed")
        try:
            pending = self.env["prema.dispatch.location"].create({
                "name": "Pending dock (test)",
                "verification_state": "pending_review"})
            verified = self.env["prema.dispatch.location"].create({
                "name": "Verified dock (test)",
                "verification_state": "verified"})
        except ValidationError:
            self.skipTest("prema.dispatch.location creation requires "
                          "fields this test DB lacks")
        stop = self._stop(saved_location_id=pending.id)
        self.assertEqual(stop.status, "new_pending")
        stop.saved_location_id = verified.id
        self.assertEqual(stop.status, "saved_reused")
