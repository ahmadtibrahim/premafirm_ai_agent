import json

from odoo import models
from odoo.exceptions import UserError


class PremaLLMService(models.AbstractModel):
    _name = "prema.llm.service"
    _description = "Prema LLM Orchestrator"

    def process(self, message, schema=None, diagnostics=None):
        self.env["prema.performance.guard"].assert_rate_limit(self.env.user.id)

        tools = self.env["prema.service.tool.registry"].get_tool_definitions()

        config = self.env["prema.config.service"]
        payload = {
            "model": config.get("prema_ai_auditor.openai_model", env_key="OPENAI_MODEL", default="gpt-4.1"),
            "messages": [
                {"role": "system", "content": "You are a controlled enterprise Odoo auditor."},
                {"role": "user", "content": message},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens": 700,
            "metadata": {
                "schema": schema or {},
                "diagnostics": diagnostics or [],
            },
        }

        response = self.env["prema.openai.client"].call(payload, timeout=45, retries=2)

        msg = response["choices"][0]["message"]
        reply = msg.get("content", "")

        if msg.get("tool_calls"):
            tool_results = []
            for call in msg["tool_calls"]:
                tool_name = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
                result = self.env["prema.service.tool.registry"].execute(tool_name, args)
                tool_results.append({"tool": tool_name, "result": result})
            reply = "Audit executed. See findings."
            return {"reply": reply, "tool_results": tool_results}

        self.env["prema.ai.audit.log"].create(
            {
                "action": f"llm_call:{self.env.user.id}",
                "status": "success",
                "details": "LLM processed message.",
            }
        )

        if not reply:
            raise UserError("Empty response from LLM.")

        return {"reply": reply}
