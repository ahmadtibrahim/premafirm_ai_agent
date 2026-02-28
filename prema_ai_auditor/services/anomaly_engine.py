from odoo import models


class PremaServiceAnomalyEngine(models.AbstractModel):
    _name = "prema.service.anomaly.engine"
    _description = "Prema Service Anomaly Engine"

    def summarize(self):
        return {"status": "ok"}
