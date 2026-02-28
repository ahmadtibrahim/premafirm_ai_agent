from odoo import models


class AIErrorMonitor(models.AbstractModel):
    _name = "prema.ai.error.monitor"
    _description = "Prema AI Error Monitor"

    def recent_errors(self, limit=20):
        logs = self.env["ir.logging"].search(
            [("level", "in", ["ERROR", "CRITICAL"])],
            order="create_date desc",
            limit=limit,
        )
        return [{"type": "server_error", "message": log.message or ""} for log in logs]

    def scan_errors(self):
        return self.recent_errors()

    def scan_and_push(self):
        errors = self.scan_errors()
        for error in errors:
            self.env["prema.ai.realtime"].push_event("error", error)
        return len(errors)
