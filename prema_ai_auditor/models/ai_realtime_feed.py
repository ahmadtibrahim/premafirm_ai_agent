from odoo import models


class AIRealtimeFeed(models.AbstractModel):
    _name = "prema.ai.realtime"
    _description = "Prema AI Realtime Event Feed"

    def push_event(self, event_type, payload):
        self.env["bus.bus"]._sendone(
            self.env.user.partner_id,
            "prema_ai_channel",
            {
                "type": event_type,
                "payload": payload,
            },
        )
