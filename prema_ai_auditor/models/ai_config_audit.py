from odoo import models


class AIConfigAudit(models.AbstractModel):
    _name = "prema.ai.config.audit"
    _description = "Prema AI Pre-Deployment Configuration Audit"

    def run(self):
        issues = []
        config = self.env["prema.config.service"]

        if not config.get("web.base.url", env_key="WEB_BASE_URL"):
            issues.append("Missing base URL")

        if not config.get("mail.catchall.domain", env_key="MAIL_CATCHALL_DOMAIN"):
            issues.append("Mail catchall not configured")

        if not config.get("openai.api_key", env_key="OPENAI_API_KEY"):
            issues.append("OpenAI API key missing")

        inactive_crons = self.env["ir.cron"].search([("active", "=", False)], limit=1)
        if inactive_crons:
            issues.append("Inactive scheduled actions detected")

        return issues
