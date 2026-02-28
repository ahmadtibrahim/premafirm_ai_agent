from odoo import models


class RealtimeNotifier(models.AbstractModel):
    _name = "prema.ai.realtime.notifier"
    _description = "Prema AI Realtime Notification Service"

    def notify_error(self, payload):
        self.env["prema.ai.realtime"].push_event("error", payload)

    def notify_performance(self, payload):
        self.env["prema.ai.realtime"].push_event("performance", payload)
    def notify_incident(self, incident):
        payload = {
            "id": incident.id,
            "name": incident.name,
            "severity": incident.severity,
            "state": incident.state,
        }
        self.env["prema.ai.realtime"].push_event("incident", payload)

