from odoo.tests.common import TransactionCase


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
                "billing_mode": "flat",
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
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 4,
                "stop_type": "delivery",
                "address": "6350 Tomken Rd, Mississauga, ON, Canada",
                "country": "Canada",
                "pallets": 9,
            }
        )
        return lead

    def test_load_number_is_grouped_by_pickup_delivery_pairs(self):
        lead = self._create_lead_with_two_loads()
        stops = lead.dispatch_stop_ids.sorted("sequence")

        self.assertEqual(stops.mapped("load_number"), [1, 1, 2, 2])

    def test_create_sales_order_creates_one_service_line_per_load(self):
        lead = self._create_lead_with_two_loads()

        action = lead.action_create_sales_order()
        order = self.env["sale.order"].browse(action["res_id"])
        service_lines = order.order_line.filtered(lambda l: l.product_id == lead.product_id)

        self.assertEqual(len(service_lines), 2)
        self.assertEqual(service_lines.mapped("name"), ["Load #1", "Load #2"])
        self.assertEqual(round(sum(service_lines.mapped("price_unit")), 2), 500.0)
