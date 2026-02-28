from odoo import fields, models


class PremaConfigAuditWizard(models.TransientModel):
    _name = "prema.config.audit.wizard"
    _description = "Prema Config Audit Wizard"

    result = fields.Text(readonly=True)

    def action_run_audit(self):
        config = self.env["prema.config.service"]
        checks = [
            ("openai.api_key", "OPENAI_API_KEY", True),
            ("prema_ai_auditor.openai_model", "OPENAI_MODEL", False),
            ("web.base.url", "WEB_BASE_URL", True),
        ]
        lines = []
        for param_key, env_key, required in checks:
            value = config.get(param_key, env_key=env_key, default="")
            masked = config.mask_secret(value) if "key" in param_key else (value or "")
            status = "OK" if value else ("MISSING" if required else "OPTIONAL_MISSING")
            lines.append(f"{param_key}: {status} ({masked})")
        self.result = "\n".join(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }
