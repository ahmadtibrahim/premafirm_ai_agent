from odoo import models


class AISelfHeal(models.AbstractModel):
    _name = "prema.ai.self.heal"
    _description = "Prema AI Self Heal"

    def diagnose(self):
        issues = []

        crons = self.env["ir.cron"].search([("active", "=", True)], limit=50)
        issues.append({"type": "cron_status", "count": len(crons)})

        issues.extend(self.env["prema.ai.error.monitor"].recent_errors(limit=20))

        failed_mails = self.env["prema.ai.mail.monitor"].failed_mails()
        if failed_mails:
            issues.append(failed_mails)

        module_states = self.env["prema.ai.integrity.engine"].module_states()
        issues.append({"type": "module_states", "items": module_states})
        return issues
