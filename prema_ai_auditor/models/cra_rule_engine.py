from odoo import models


class CRARuleEngine(models.AbstractModel):
    _name = "prema.cra.engine"
    _description = "Canadian CRA Rule Engine"

    def validate_gst_integrity(self):
        bills = self.env["account.move"].search(
            [("move_type", "=", "in_invoice"), ("state", "=", "posted")]
        )

        for bill in bills:
            if bill.amount_tax > 0 and not bill.invoice_pdf_report_id:
                self.env["prema.audit.log"].create(
                    {
                        "rule_name": "Missing GST Invoice",
                        "severity": "high",
                        "model_name": "account.move",
                        "record_id": bill.id,
                        "explanation": "GST claimed without invoice attachment",
                    }
                )

    def validate_zero_tax_vendor(self):
        vendors = self.env["res.partner"].search(
            [("supplier_rank", ">", 0), ("country_id.code", "=", "CA")]
        )

        for vendor in vendors:
            moves = self.env["account.move"].search(
                [("partner_id", "=", vendor.id), ("amount_tax", "=", 0)]
            )
            if moves:
                self.env["prema.audit.log"].create(
                    {
                        "rule_name": "Zero Tax Canadian Vendor",
                        "severity": "medium",
                        "model_name": "res.partner",
                        "record_id": vendor.id,
                        "explanation": "Canadian vendor invoicing without tax",
                    }
                )
