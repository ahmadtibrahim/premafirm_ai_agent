from lxml import etree

from odoo.tests.common import TransactionCase


class TestCrmLeadPricingRegression(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Pricing Customer"})

    def test_crm_lead_form_view_and_pricing_fields_work_without_legacy_discount_fields(self):
        lead = self.env["crm.lead"].create(
            {
                "name": "Pricing Lead",
                "partner_id": self.partner.id,
                "suggested_rate": 550.0,
                "final_rate": 600.0,
            }
        )

        # Loading form architecture should not fail on removed fields.
        view_info = self.env["crm.lead"].fields_view_get(view_type="form")
        etree.fromstring(view_info["arch"].encode())

        lead.flush_recordset(["final_rate_total"])
        self.assertEqual(lead.final_rate_total, 600.0)
        self.assertEqual(lead.suggested_rate, 550.0)
