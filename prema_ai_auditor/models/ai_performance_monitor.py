from datetime import timedelta

from odoo import fields, models


class AIPerformanceMonitor(models.AbstractModel):
    _name = "prema.ai.performance"
    _description = "Prema AI Performance Bottleneck Analyzer"

    def analyze(self):
        since = fields.Datetime.now() - timedelta(hours=1)
        slow_logs = self.env["ir.logging"].search(
            [
                ("message", "ilike", "slow"),
                ("create_date", ">", since),
            ]
        )

        heavy_models = {}
        for log in slow_logs:
            source = log.name or "unknown"
            heavy_models[source] = heavy_models.get(source, 0) + 1

        return heavy_models
