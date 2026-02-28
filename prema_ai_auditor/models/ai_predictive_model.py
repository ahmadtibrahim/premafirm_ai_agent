from datetime import timedelta

from odoo import fields, models


class AIPredictiveModel(models.AbstractModel):
    _name = "prema.ai.predictive"
    _description = "Prema AI Predictive Failure Model"

    def predict_failure(self):
        errors = self.env["ir.logging"].search(
            [("level", "in", ["ERROR", "CRITICAL"])],
            limit=200,
            order="create_date desc",
        )
        error_frequency = len(errors)

        one_hour_ago = fields.Datetime.now() - timedelta(hours=1)
        recent_errors = sum(1 for log in errors if log.create_date and log.create_date >= one_hour_ago)

        if error_frequency > 50 or recent_errors > 20:
            return {
                "risk_level": "high",
                "prediction": "System instability likely",
                "error_frequency": error_frequency,
                "recent_error_frequency": recent_errors,
            }

        return {
            "risk_level": "normal",
            "error_frequency": error_frequency,
            "recent_error_frequency": recent_errors,
        }
