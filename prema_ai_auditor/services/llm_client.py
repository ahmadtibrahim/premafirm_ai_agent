import json

import requests

from odoo import models
from odoo.exceptions import UserError


class PremaLLMClient(models.AbstractModel):
    _name = "prema.llm.client"
    _description = "Prema LLM Client"

    def call(self, prompt, timeout=20):
        config = self.env["prema.config.service"]
        provider = config.get("prema_ai.llm.provider", default="openai")
        model = config.get("prema_ai.openai.model", default="gpt-4.1")

        if provider != "openai":
            raise UserError("Unsupported LLM provider configured.")

        api_key = config.require("prema_ai.openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API Key")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = requests.post(
            config.get("prema_ai.openai.endpoint", default="https://api.openai.com/v1/chat/completions"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise UserError("LLM call failed; check provider settings and connectivity.")
        return response.json()
