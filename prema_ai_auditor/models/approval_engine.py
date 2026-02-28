from odoo import fields, models


class ApprovalEngine(models.AbstractModel):
    _name = "prema.approval.engine"
    _description = "Prema Approval Engine"

    def approve_fix(self, log_id):
        log = self.env["prema.audit.log"].browse(log_id)

        if log.status != "open":
            return

        log.status = "approved"
        log.approved_by = self.env.user.id
        log.approved_at = fields.Datetime.now()

        # Controlled execution only here
