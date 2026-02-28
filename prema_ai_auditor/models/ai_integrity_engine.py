from odoo import models


class AIIntegrityEngine(models.AbstractModel):
    _name = "prema.ai.integrity.engine"
    _description = "Prema AI Integrity Engine"

    def module_states(self):
        modules = self.env["ir.module.module"].search([("name", "=", "prema_ai_auditor")], limit=1)
        return [{"module": mod.name, "state": mod.state} for mod in modules]
