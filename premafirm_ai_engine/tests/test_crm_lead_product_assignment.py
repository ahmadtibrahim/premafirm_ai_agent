from odoo.tests.common import TransactionCase


class TestCrmLeadProductAssignment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.country_ca = self.env.ref("base.ca")
        self.country_us = self.env.ref("base.us")
        self.partner_ca = self.env["res.partner"].create({"name": "Customer CA", "country_id": self.country_ca.id})
        self.partner_us = self.env["res.partner"].create({"name": "Customer US", "country_id": self.country_us.id})

    def _create_lead(self, partner):
        return self.env["crm.lead"].create({"name": "Lead", "partner_id": partner.id})

    def test_assign_stop_products_ftl_two_stop_structure(self):
        lead = self._create_lead(self.partner_ca)
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 1,
                "stop_type": "pickup",
                "address": "Toronto, Canada",
                "country": "Canada",
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 2,
                "stop_type": "delivery",
                "address": "Montreal, Canada",
                "country": "Canada",
            }
        )

        lead._assign_stop_products()

        for stop in lead.dispatch_stop_ids:
            self.assertTrue(stop.is_ftl)
            self.assertEqual(stop.product_id.product_tmpl_id.name, "FTL Freight Service - Canada")

    def test_assign_stop_products_ltl_for_multi_delivery(self):
        lead = self._create_lead(self.partner_us)
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 1,
                "stop_type": "pickup",
                "address": "Chicago, USA",
                "country": "US",
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 2,
                "stop_type": "delivery",
                "address": "Dallas, USA",
                "country": "US",
            }
        )
        self.env["premafirm.dispatch.stop"].create(
            {
                "lead_id": lead.id,
                "sequence": 3,
                "stop_type": "delivery",
                "address": "Houston, USA",
                "country": "US",
            }
        )

        lead._assign_stop_products()

        for stop in lead.dispatch_stop_ids:
            self.assertFalse(stop.is_ftl)
            self.assertEqual(stop.product_id.product_tmpl_id.name, "LTL - Freight Service - USA")
