from odoo.tests.common import TransactionCase

from ..services.ai_extraction_service import AIExtractionService
from ..services.crm_dispatch_service import CRMDispatchService


class TestCrmLoadNumberAndSalesOrderLines(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create(
            {
                "name": "Customer CA",
                "country_id": self.env.ref("base.ca").id,
            }
        )

    def _create_lead_with_two_loads(self):
        lead = self.env["crm.lead"].create(
            {
                "name": "Two Load Lead",
                "partner_id": self.partner.id,
                "final_rate": 500.0,
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 1,
                "stop_type": "pickup",
                "address": "55 Commerce Park Dr, Barrie, ON, Canada",
                "country": "Canada",
                "pallets": 8,
                "distance_km": 0.0,
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 2,
                "stop_type": "delivery",
                "address": "6350 Tomken Rd, Mississauga, ON, Canada",
                "country": "Canada",
                "pallets": 8,
                "distance_km": 100.0,
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 3,
                "stop_type": "pickup",
                "address": "7100 Martin Grove Rd, Vaughan, ON, Canada",
                "country": "Canada",
                "pallets": 9,
                "distance_km": 0.0,
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 4,
                "stop_type": "delivery",
                "address": "120 Industry St, Ottawa, ON, Canada",
                "country": "Canada",
                "pallets": 9,
                "distance_km": 300.0,
            }
        )
        return lead

    def test_load_assignment_grouped_by_pickup_delivery_pairs(self):
        lead = self._create_lead_with_two_loads()
        stops = lead.dispatch_stop_ids.sorted("sequence")

        self.assertEqual(len(set(stops.mapped("load_id").ids)), 2)
        self.assertEqual(stops[0].load_id, stops[1].load_id)
        self.assertEqual(stops[2].load_id, stops[3].load_id)
        self.assertNotEqual(stops[1].load_id, stops[2].load_id)

    def test_load_id_manual_reassignment_logs_ai_correction(self):
        lead = self._create_lead_with_two_loads()
        stops = lead.dispatch_stop_ids.sorted("sequence")
        target_load = stops[0].load_id

        stops[3].write({"load_id": target_load.id})

        correction = self.env["premafirm.ai.correction"].search([("stop_id", "=", stops[3].id)], limit=1)
        self.assertTrue(correction)
        self.assertEqual(correction.lead_id, lead)
        self.assertEqual(correction.new_load_id, target_load)

    def test_create_sales_order_creates_one_service_line_per_load_with_km_allocation(self):
        lead = self._create_lead_with_two_loads()

        action = lead.action_create_sales_order()
        order = self.env["sale.order"].browse(action["res_id"])
        service_lines = order.order_line.filtered(lambda l: l.product_id == lead.product_id)

        self.assertEqual(len(service_lines), 2)
        self.assertEqual(round(sum(service_lines.mapped("price_unit")), 2), 500.0)
        self.assertEqual(sorted(round(x, 2) for x in service_lines.mapped("price_unit")), [125.0, 375.0])
        self.assertTrue(all(service_lines.mapped("load_id")))

    def test_schedule_defaults_to_vehicle_work_start_hour(self):
        vehicle = self.env["fleet.vehicle"].create({"name": "Truck 09", "vehicle_work_start_time": 9.0})
        lead = self.env["crm.lead"].create({"name": "Schedule Lead", "partner_id": self.partner.id, "assigned_vehicle_id": vehicle.id})
        stop_1 = self.env["premafirm.dispatch.stop"].create(
            {"lead_id": lead.id, "sequence": 1, "stop_type": "pickup", "address": "Toronto, Canada"}
        )
        stop_2 = self.env["premafirm.dispatch.stop"].create(
            {"lead_id": lead.id, "sequence": 2, "stop_type": "delivery", "address": "Ottawa, Canada"}
        )

        service = CRMDispatchService(self.env)
        service._compute_stop_schedule(lead.dispatch_stop_ids.sorted("sequence"), [{"drive_hours": 1.0}, {"drive_hours": 2.0}])

        self.assertEqual(stop_1.scheduled_datetime.hour, 9)
        self.assertTrue(lead.leave_yard_at)

    def test_weather_fields_populate_and_severe_sets_schedule_conflict(self):
        lead = self.env["crm.lead"].create({"name": "Weather Lead", "partner_id": self.partner.id})
        self.env["premafirm.dispatch.stop"].create(
            {"lead_id": lead.id, "sequence": 1, "stop_type": "pickup", "address": "Snow Valley, Canada"}
        )

        service = CRMDispatchService(self.env)
        risk, advisories = service._compute_weather_risk(lead)

        self.assertEqual(risk, "severe")
        self.assertTrue(advisories)
        self.assertEqual(lead.weather_alert_level, "severe")
        self.assertTrue(lead.schedule_conflict)
        self.assertTrue(lead.weather_summary)


    def test_parse_load_sections_handles_pickup_delivery_information_headings(self):
        service = AIExtractionService(env=None)
        raw_text = """
LOAD #1
Pickup Information
Barrie, ON

Delivery Information
Mississauga, ON

Pallets: 8
Weight: 9115 lbs
"""

        parsed = service._parse_load_sections(raw_text)

        self.assertEqual(len(parsed["stops"]), 2)
        self.assertEqual(parsed["stops"][0]["stop_type"], "pickup")
        self.assertTrue(parsed["stops"][0]["address"].startswith("Barrie"))
        self.assertEqual(parsed["stops"][1]["stop_type"], "delivery")
        self.assertTrue(parsed["stops"][1]["address"].startswith("Mississauga"))


    def test_normalize_stop_values_accepts_weight_alias(self):
        service = CRMDispatchService(self.env)
        stops = service._normalize_stop_values(
            [
                {
                    "sequence": 1,
                    "stop_type": "pickup",
                    "address": "Barrie, ON",
                    "pallets": 8,
                    "weight": 9115,
                },
                {
                    "sequence": 2,
                    "stop_type": "delivery",
                    "address": "Mississauga, ON",
                    "pallets": 8,
                    "weight": 9115,
                },
            ]
        )

        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0]["weight_lbs"], 9115.0)
        self.assertEqual(stops[1]["weight_lbs"], 9115.0)
    def test_action_create_sales_order_recreates_after_delete(self):
        lead = self._create_lead_with_two_loads()

        first_action = lead.action_create_sales_order()
        first_order = self.env["sale.order"].browse(first_action["res_id"])
        first_order.unlink()

        second_action = lead.action_create_sales_order()
        second_order = self.env["sale.order"].browse(second_action["res_id"])

        self.assertTrue(second_order.exists())
        self.assertNotEqual(first_order.id, second_order.id)
        self.assertEqual(second_order.state, "draft")
