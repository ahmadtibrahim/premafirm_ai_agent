from odoo import fields
from odoo.tests.common import TransactionCase

from ..services.run_planner_service import RunPlannerService


class TestRunPlannerService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Driver Partner"})
        self.vehicle_model = self.env["fleet.vehicle.model"].create(
            {"name": "Test Model", "brand_id": self.env["fleet.vehicle.model.brand"].create({"name": "Test Brand"}).id}
        )
        self.vehicle = self.env["fleet.vehicle"].create(
            {
                "name": "Truck 1",
                "license_plate": "ABC123",
                "model_id": self.vehicle_model.id,
                "driver_id": self.partner.id,
            }
        )
        self.run = self.env["premafirm.dispatch.run"].create(
            {
                "name": "Run Truck 1",
                "vehicle_id": self.vehicle.id,
                "run_date": fields.Date.today(),
            }
        )

    def test_update_run_with_partner_driver_creates_calendar_event(self):
        planner = RunPlannerService(self.env)
        simulation = {
            "total_drive_hours": 1.5,
            "total_distance_km": 120.0,
            "empty_distance_km": 25.0,
            "loaded_distance_km": 95.0,
        }

        planner._update_run(self.run, simulation)
        self.assertTrue(self.run.calendar_event_id)
        self.assertEqual(self.run.calendar_event_id.partner_ids, self.partner)
