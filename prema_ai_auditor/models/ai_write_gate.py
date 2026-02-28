from odoo import fields, models
from odoo.exceptions import UserError


class AIWriteGate(models.AbstractModel):
    _name = "prema.ai.write.gate"
    _description = "Prema AI Write Gate"

    def execute(self, token):
        req = self.env["prema.ai.action.request"].search(
            [
                ("token", "=", token),
                ("approved", "=", True),
                ("executed", "=", False),
            ],
            limit=1,
        )

        if not req:
            raise UserError("Invalid or expired approval.")

        if fields.Datetime.now() > req.expires_at:
            raise UserError("Approval expired.")

        record = self.env[req.model_name].browse(req.record_id)
        if not record.exists():
            raise UserError("Target record does not exist.")

        if req.action_type != "write":
            raise UserError("Unsupported action type.")

        record.write(req.payload_json)
        req.executed = True

        self.env["prema.ai.audit.log"].create(
            {
                "action": "write_gate_execute",
                "status": "success",
                "details": "Approved write executed.",
                "action_request_id": req.id,
                "model_name": req.model_name,
                "record_id": req.record_id,
            }
        )
        return True
