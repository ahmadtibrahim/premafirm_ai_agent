from odoo import models


class ERPIntegrityScanner(models.AbstractModel):
    _name = "prema.integrity.scanner"
    _description = "ERP Integrity Scanner"

    def run_full_scan(self):
        self._scan_account_integrity()
        self._scan_partner_integrity()
        self._scan_inventory_integrity()
        self._scan_fleet_integrity()
        self._scan_crm_integrity()

    def _scan_account_integrity(self):
        accounts = self.env["account.account"].search([])
        for acc in accounts:
            if not acc.account_type:
                self._log("account.account", acc.id, "high", "Account missing type")

    def _scan_partner_integrity(self):
        partners = self.env["res.partner"].search([])
        for partner in partners:
            if partner.supplier_rank and not partner.vat:
                self._log("res.partner", partner.id, "medium", "Vendor missing tax number")

    def _scan_inventory_integrity(self):
        products = self.env["product.template"].search([])
        for product in products:
            if product.standard_price == 0:
                self._log("product.template", product.id, "low", "Product has zero cost")

    def _scan_fleet_integrity(self):
        vehicles = self.env["fleet.vehicle"].search([])
        for vehicle in vehicles:
            if not vehicle.driver_id:
                self._log("fleet.vehicle", vehicle.id, "low", "Vehicle has no assigned driver")

    def _scan_crm_integrity(self):
        leads = self.env["crm.lead"].search([])
        for lead in leads:
            if lead.stage_id.probability > 80 and not lead.expected_revenue:
                self._log(
                    "crm.lead",
                    lead.id,
                    "medium",
                    "High probability lead with no revenue",
                )

    def _log(self, model, rec_id, severity, msg):
        self.env["prema.audit.log"].create(
            {
                "rule_name": "Integrity Scan",
                "severity": severity,
                "model_name": model,
                "record_id": rec_id,
                "explanation": msg,
            }
        )
