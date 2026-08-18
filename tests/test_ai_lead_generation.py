from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestAILeadGeneration(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env["prema.ai.session"]
        cls.Partner = cls.env["res.partner"]
        cls.Lead = cls.env["crm.lead"]
        cls.admin = cls.env.ref("base.user_admin")
        cls.new_stage = cls.env["crm.stage"].search([("name", "=", "New")], limit=1)
        if not cls.new_stage:
            cls.new_stage = cls.env["crm.stage"].create({"name": "New"})

    def _new_session(self, name="Lead Gen Test"):
        return self.Session.create({"name": name, "user_id": self.admin.id})

    def test_upsert_lead_creates_company_contact_and_new_stage(self):
        session = self._new_session()
        result = session._upsert_lead_from_ai({
            "company_name": "Acme Produce Logistics",
            "contact_name": "Jane Dispatcher",
            "title": "Logistics Manager",
            "email": "jane@acmeproduce.example",
            "phone": "+1 613-555-0100",
            "website": "https://acmeproduce.example",
            "street": "123 Market St",
            "city": "Ottawa",
            "state": "ON",
            "country": "Canada",
            "description": "Google Places match for food distribution",
        })

        self.assertEqual(result["status"], "created")
        lead = result["lead"]
        company = self.Partner.search([
            ("name", "=", "Acme Produce Logistics"),
            ("is_company", "=", True),
        ])
        contact = self.Partner.search([
            ("parent_id", "=", company.id),
            ("email", "=", "jane@acmeproduce.example"),
        ])

        self.assertEqual(len(company), 1)
        self.assertEqual(len(contact), 1)
        # PHASE 11 rule: the opportunity's partner is the COMPANY — a contact
        # passed as partner_id is repointed to its parent company on create
        # (the contact stays as a res.partner child row for the Contacts tab).
        self.assertEqual(lead.partner_id, company)
        self.assertEqual(contact.parent_id, company)
        self.assertEqual(lead.partner_name, "Acme Produce Logistics")
        self.assertEqual(lead.stage_id.name, "New")
        self.assertEqual(lead.type, "opportunity")
        self.assertEqual(lead.user_id, self.admin)
        self.assertEqual(lead.website, "https://acmeproduce.example")
        self.assertEqual(lead.email_from, "jane@acmeproduce.example")

    def test_upsert_lead_reuses_existing_company_and_existing_lead(self):
        company = self.Partner.create({
            "name": "Healthy Warehouse Group",
            "is_company": True,
            "website": "https://healthywarehouse.example",
            "city": "Ottawa",
        })
        lead = self.Lead.create({
            "name": "Healthy Warehouse Group",
            "type": "opportunity",
            "partner_id": company.id,
            "partner_name": company.name,
            "stage_id": self.new_stage.id,
        })
        session = self._new_session()

        first = session._upsert_lead_from_ai({
            "company_name": "Healthy Warehouse Group",
            "contact_name": "Mark Supply",
            "title": "Dispatch Manager",
            "email": "mark@healthywarehouse.example",
            "website": "https://healthywarehouse.example",
            "city": "Ottawa",
            "state": "ON",
            "country": "Canada",
        })
        second = session._upsert_lead_from_ai({
            "company_name": "Healthy Warehouse Group",
            "contact_name": "Mark Supply",
            "title": "Dispatch Manager",
            "email": "mark@healthywarehouse.example",
            "website": "https://healthywarehouse.example",
            "city": "Ottawa",
            "state": "ON",
            "country": "Canada",
        })

        contacts = self.Partner.search([
            ("parent_id", "=", company.id),
            ("email", "=", "mark@healthywarehouse.example"),
        ])
        companies = self.Partner.search([
            ("name", "=", "Healthy Warehouse Group"),
            ("is_company", "=", True),
        ])

        self.assertEqual(first["status"], "updated")
        self.assertEqual(first["lead"], lead)
        self.assertEqual(second["status"], "existing")
        self.assertEqual(len(companies), 1)
        self.assertEqual(len(contacts), 1)

    def test_confirm_create_single_lead_keeps_remaining_pending(self):
        session = self._new_session()
        session._save_pending_leads([
            {
                "company_name": "North Route Foods",
                "contact_name": "Paul Route",
                "title": "Transportation Manager",
                "email": "paul@northroute.example",
                "website": "https://northroute.example",
                "city": "Ottawa",
                "state": "ON",
                "country": "Canada",
                "pending_key": "north-route",
            },
            {
                "company_name": "Capital Produce Depot",
                "contact_name": "Lisa Dock",
                "title": "Warehouse Manager",
                "email": "lisa@capitalproduce.example",
                "website": "https://capitalproduce.example",
                "city": "Ottawa",
                "state": "ON",
                "country": "Canada",
                "pending_key": "capital-produce",
            },
        ])

        result = self.Session.confirm_create_single_lead(session.id, "north-route")

        self.assertEqual(result["created"], 1)
        self.assertIsNotNone(result["pending_action"])
        self.assertEqual(result["pending_action"]["count"], 1)
        remaining = session._load_pending_leads()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["company_name"], "Capital Produce Depot")

    def test_industry_search_stages_ranked_pending_cards(self):
        session = self._new_session()
        plan = {
            "count": 2,
            "location": "Ottawa, ON",
            "radius_km": 100,
            "search_terms": ["food distribution", "produce wholesaler"],
            "desired_titles": ["Logistics Manager", "Dispatch Manager"],
        }
        candidates = [
            {
                "company_name": "Ottawa Fresh Foods",
                "contact_name": "Nora Chain",
                "title": "Logistics Manager",
                "email": "nora@ottawafresh.example",
                "website": "https://ottawafresh.example",
                "city": "Ottawa",
                "state": "ON",
                "country": "Canada",
                "description": "Best suggested contact for ottawafresh.example",
            },
            {
                "company_name": "Capital Distribution Hub",
                "contact_name": "Sam Dock",
                "title": "Dispatch Manager",
                "email": "sam@capitalhub.example",
                "website": "https://capitalhub.example",
                "city": "Ottawa",
                "state": "ON",
                "country": "Canada",
                "description": "Best suggested contact for capitalhub.example",
            },
        ]

        session_class = type(session)
        with patch.object(session_class, "_get_google_maps_api_key", return_value="fake-key"), \
             patch.object(session_class, "_plan_lead_generation_request", return_value=plan), \
             patch.object(session_class, "_prepare_google_lead_candidates", return_value=candidates), \
             patch.object(session_class, "_rank_lead_candidates", return_value=candidates):
            reply = session._handle_industry_lead_search(
                "Generate 2 leads in Ottawa, ON within 100 km for food distribution"
            )

        self.assertIn("Prepared 2 lead(s)", reply)
        pending = session._load_pending_leads()
        action = session._build_pending_action_payload()
        self.assertEqual(len(pending), 2)
        self.assertEqual(action["count"], 2)
        self.assertEqual(action["leads"][0]["company_name"], "Ottawa Fresh Foods")
        self.assertEqual(action["leads"][0]["contact_name"], "Nora Chain")
