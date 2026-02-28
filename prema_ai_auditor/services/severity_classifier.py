from odoo import models


class PremaSeverityClassifier(models.AbstractModel):
    _name = "prema.severity.classifier"
    _description = "Prema Severity Classifier"

    def classify(self, text):
        text_l = (text or "").lower()
        if any(token in text_l for token in ["traceback", "keyerror", "registry", "deadlock"]):
            return "critical"
        if any(token in text_l for token in ["timeout", "failed", "error"]):
            return "high"
        if any(token in text_l for token in ["warning", "slow"]):
            return "medium"
        return "low"
