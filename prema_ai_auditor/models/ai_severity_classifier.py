from odoo import models


class AISeverityClassifier(models.AbstractModel):
    _name = "prema.ai.severity"
    _description = "Prema AI Incident Severity Classifier"

    def classify(self, issue):
        content = (issue or "").lower()
        keywords_critical = ["database", "corruption", "traceback"]
        keywords_high = ["timeout", "mail failure"]

        for keyword in keywords_critical:
            if keyword in content:
                return "critical"

        for keyword in keywords_high:
            if keyword in content:
                return "high"

        return "medium"
