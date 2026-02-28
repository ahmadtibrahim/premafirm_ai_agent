from odoo import models


class ReconciliationAdvisor(models.AbstractModel):
    _name = "prema.reconcile.advisor"
    _description = "Multi-bank Reconciliation Advisor"

    def suggest_matches(self):
        lines = self.env["account.bank.statement.line"].search(
            [("is_reconciled", "=", False)]
        )

        for line in lines:
            candidates = self.env["account.move"].search(
                [("amount_total", "=", line.amount)], limit=1
            )

            if candidates:
                self.env["prema.audit.log"].create(
                    {
                        "rule_name": "Reconciliation Suggestion",
                        "severity": "low",
                        "model_name": "account.bank.statement.line",
                        "record_id": line.id,
                        "explanation": f"Possible match: {candidates.name}",
                    }
                )
