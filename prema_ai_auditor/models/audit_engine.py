from odoo import models


class PremaAuditEngine(models.AbstractModel):
    _name = "prema.audit.engine"
    _description = "Prema AI Audit Engine"

    def run_default_scan(self):
        registry = self.env["prema.tool.registry"]
        duplicate_bill_ids = registry.execute("scan_duplicate_bills", {})
        for move_id in duplicate_bill_ids:
            self.env["prema.audit.log"].create(
                {
                    "rule_name": "Duplicate Vendor Bill",
                    "severity": "high",
                    "model_name": "account.move",
                    "record_id": move_id,
                    "explanation": "Multiple posted vendor bills match partner, date, and total.",
                }
            )
        return {"duplicate_bills": duplicate_bill_ids}
