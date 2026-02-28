from odoo import models


class AIDashboardMetrics(models.Model):
    _inherit = "prema.ai.dashboard"

    def severity_counts(self):
        logs = self.env["prema.audit.log"].search([("status", "=", "open")])
        counts = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
        }

        for log in logs:
            counts[log.severity] = counts.get(log.severity, 0) + 1

        return counts
