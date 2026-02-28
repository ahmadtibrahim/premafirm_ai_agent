from odoo import models
from odoo.exceptions import UserError


class PremaServiceToolRegistry(models.AbstractModel):
    _name = "prema.service.tool.registry"
    _description = "Prema Service Tool Registry"

    def get_tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "diagnose_system",
                    "description": "Run self-heal diagnostics and return a summarized issue list.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def execute(self, tool_name, args):
        tools = {"diagnose_system": self._diagnose_system}
        if tool_name not in tools:
            raise UserError("Unauthorized tool call")
        try:
            return tools[tool_name](**args)
        except Exception as exc:
            self.env["prema.ai.audit.log"].create(
                {
                    "action": "tool_execution",
                    "status": "failed",
                    "details": f"{tool_name}: {exc}",
                }
            )
            raise

    def _diagnose_system(self):
        return self.env["prema.ai.self.heal"].diagnose()
