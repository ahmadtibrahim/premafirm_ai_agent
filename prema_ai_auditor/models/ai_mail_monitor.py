from odoo import models


class AIMailMonitor(models.AbstractModel):
    _name = "prema.ai.mail.monitor"
    _description = "Prema AI Mail Monitor"

    def failed_mails(self):
        failed = self.env["mail.mail"].search([("state", "=", "exception")], limit=200)
        return {"type": "mail_failure", "count": len(failed)} if failed else {}
