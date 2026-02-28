from odoo import api, fields, models


class PremaAIIncident(models.Model):
    _name = "prema.ai.incident"
    _description = "Prema AI Incident"
    _order = "create_date desc"

    name = fields.Char(required=True)
    source = fields.Selection(
        [("ir_logging", "IR Logging"), ("cron", "Cron"), ("mail", "Mail"), ("diagnostics", "Diagnostics")],
        required=True,
        default="ir_logging",
    )
    severity = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")],
        required=True,
        default="medium",
    )
    error_signature = fields.Char(index=True)
    details = fields.Text()
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True, index=True)
    state = fields.Selection(
        [("open", "Open"), ("triaged", "Triaged"), ("resolved", "Resolved")], default="open", required=True
    )

    @api.model
    def create_from_log(self, log_record):
        vals = {
            "name": log_record.name or "Unhandled incident",
            "source": "ir_logging",
            "severity": self.env["prema.severity.classifier"].classify(log_record.message or ""),
            "error_signature": f"{log_record.name}:{log_record.level}",
            "details": log_record.message,
            "company_id": self.env.company.id,
        }
        incident = self.create(vals)
        self.env["prema.ai.realtime.notifier"].notify_incident(incident)
        return incident
