from odoo import models


class RiskMatrix(models.AbstractModel):
    _name = "prema.risk.matrix"
    _description = "Risk Matrix"

    def calculate_risk_score(self):
        logs = self.env["prema.audit.log"].search([("status", "=", "open")])

        weight_map = {
            "critical": 20,
            "high": 10,
            "medium": 5,
            "low": 2,
        }

        return sum(weight_map.get(log.severity, 1) for log in logs)
