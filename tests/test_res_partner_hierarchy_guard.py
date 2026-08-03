from odoo.tests.common import TransactionCase


class TestResPartnerHierarchyGuard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.partner"].create({
            "name": "Parent Company",
            "is_company": True,
        })
        cls.person_parent = cls.env["res.partner"].create({
            "name": "Existing Person",
            "parent_id": cls.company.id,
        })

    def test_create_flattens_person_parent_to_company(self):
        child = self.env["res.partner"].create({
            "name": "Nested Person",
            "parent_id": self.person_parent.id,
        })
        self.assertEqual(child.parent_id, self.company)

    def test_write_flattens_person_parent_to_company(self):
        child = self.env["res.partner"].create({
            "name": "Writable Person",
        })
        child.write({"parent_id": self.person_parent.id})
        self.assertEqual(child.parent_id, self.company)

    def test_create_flattens_through_misclassified_company_chain(self):
        fake_company = self.env["res.partner"].create({
            "name": "Fake Company Person",
            "is_company": True,
            "parent_id": self.person_parent.id,
        })
        child = self.env["res.partner"].create({
            "name": "Nested Through Fake Company",
            "parent_id": fake_company.id,
        })
        self.assertEqual(child.parent_id, self.company)
