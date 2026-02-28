import statistics

from odoo import models


class AnomalyEngine(models.AbstractModel):
    _name = "prema.anomaly.engine"
    _description = "Intelligent Anomaly Scoring"

    def detect_outliers(self):
        amounts = []
        moves = self.env["account.move"].search([("move_type", "=", "in_invoice")])

        for move in moves:
            amounts.append(move.amount_total)

        if len(amounts) < 5:
            return

        average = statistics.mean(amounts)
        stddev = statistics.stdev(amounts)

        for move in moves:
            if abs(move.amount_total - average) > 2 * stddev:
                self.env["prema.audit.log"].create(
                    {
                        "rule_name": "Amount Outlier",
                        "severity": "medium",
                        "model_name": "account.move",
                        "record_id": move.id,
                        "explanation": "Invoice amount outside statistical range",
                    }
                )
