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

    def test_update_run_without_driver_partner_still_creates_calendar_event(self):
        planner = RunPlannerService(self.env)
        self.vehicle.driver_id = False
        simulation = {
            "total_drive_hours": 0.75,
            "total_distance_km": 42.0,
            "empty_distance_km": 10.0,
            "loaded_distance_km": 32.0,
        }

        planner._update_run(self.run, simulation)

        self.assertTrue(self.run.calendar_event_id)
        self.assertFalse(self.run.calendar_event_id.partner_ids)

    def test_update_run_uses_stop_schedule_window_for_calendar_event(self):
        lead = self.env["crm.lead"].create(
            {
                "name": "Scheduled Lead",
                "assigned_vehicle_id": self.vehicle.id,
                "partner_id": self.env["res.partner"].create({"name": "Customer"}).id,
            }
        )
        lead.leave_yard_at = fields.Datetime.to_datetime("2026-02-25 08:00:00")
        stop_1 = self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 1,
                "stop_type": "pickup",
                "address": "Barrie, ON",
                "pallets": 9,
                "weight_lbs": 9000,
                "scheduled_datetime": "2026-02-25 10:05:00",
                "scheduled_end_datetime": "2026-02-25 10:35:00",
            }
        )
        stop_2 = self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 2,
                "stop_type": "delivery",
                "address": "Mississauga, ON",
                "pallets": 9,
                "weight_lbs": 9000,
                "scheduled_datetime": "2026-02-25 11:47:00",
                "scheduled_end_datetime": "2026-02-25 12:17:00",
            }
        )
        stop_1.write({"run_id": self.run.id, "run_sequence": 1})
        stop_2.write({"run_id": self.run.id, "run_sequence": 2})

        planner = RunPlannerService(self.env)
        simulation = {
            "total_drive_hours": 1.5,
            "total_distance_km": 120.0,
            "empty_distance_km": 25.0,
            "loaded_distance_km": 95.0,
        }

        planner._update_run(self.run, simulation)

        self.assertEqual(self.run.start_datetime, fields.Datetime.to_datetime("2026-02-25 08:00:00"))
        self.assertEqual(self.run.end_datetime, fields.Datetime.to_datetime("2026-02-25 12:17:00"))
        self.assertEqual(self.run.calendar_event_id.start, self.run.start_datetime)
        self.assertEqual(self.run.calendar_event_id.stop, self.run.end_datetime)
