from odoo import fields, models


class PremaAIDashboard(models.Model):
    _name = "prema.ai.dashboard"
    _description = "Prema AI Dashboard"

    name = fields.Char(default="Prema AI Dashboard")
    health_score = fields.Integer(compute="_compute_health_score")
    incident_ids = fields.Many2many("prema.ai.incident", compute="_compute_incident_ids", readonly=True)

    def _compute_health_score(self):
        score_engine = self.env["prema.health.score"]
        for record in self:
            record.health_score = score_engine.compute_score()

    def _compute_incident_ids(self):
        incidents = self.env["prema.ai.incident"].search([], order="create_date desc", limit=20)
        for record in self:
            record.incident_ids = incidents

    def action_open_schema_viewer(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Schema Viewer",
            "res_model": "prema.schema.viewer.wizard",
            "view_mode": "form",
            "target": "new",
        }

    def action_open_ai_chat(self):
        return {
            "type": "ir.actions.client",
            "tag": "prema_ai_chat_action",
            "context": {
                "ai_enabled": True,
                "model_name": "account.move",
                "auto_fix": False,
            },
        }
