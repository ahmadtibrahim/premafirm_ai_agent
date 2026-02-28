from odoo import models
from odoo.exceptions import UserError


class PremaWriteGateService(models.AbstractModel):
    _name = "prema.write.gate.service"
    _description = "Prema Write Gate Service"

    def apply_proposal(self, proposal_id):
        proposal = self.env["prema.ai.proposal"].browse(proposal_id)
        if not proposal.exists():
            raise UserError("Proposal not found.")
        if proposal.status != "approved":
            raise UserError("Proposal must be approved before apply.")
        proposal.action_apply()
        self.env["prema.ai.audit.log"].sudo().create(
            {
                "action": "apply_proposal",
                "status": "success",
                "details": f"Applied proposal {proposal.id}",
                "model_name": proposal.target_model,
                "record_id": proposal.target_res_id,
            }
        )
        return True
