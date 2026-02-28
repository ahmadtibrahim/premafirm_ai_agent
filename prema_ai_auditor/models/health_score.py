from odoo import models


class HealthScore(models.AbstractModel):
    _name = "prema.health.score"
    _description = "Prema Health Score Engine"

    def compute_score(self):
        logs = self.env["prema.audit.log"].search([("status", "=", "open")])

        score = 100
        for log in logs:
            weight = {
                "critical": 10,
                "high": 5,
                "medium": 3,
                "low": 1,
            }.get(log.severity, 1)
            score -= weight

        return max(score, 0)
