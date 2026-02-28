from odoo import fields, models
from odoo.exceptions import UserError


class PremaAIProposal(models.Model):
    _name = "prema.ai.proposal"
    _description = "Prema AI Proposal"
    _order = "create_date desc"

    name = fields.Char(required=True, default="AI Proposal")
    created_by = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, readonly=True)
    target_model = fields.Char(required=True)
    target_res_id = fields.Integer(default=0)
    action_type = fields.Selection(
        [("write", "Write"), ("create", "Create"), ("link", "Link"), ("reconcile", "Reconcile")],
        required=True,
        default="write",
    )
    payload_json = fields.Json(required=True)
    rationale = fields.Text()
    risk_score = fields.Integer(default=0)
    severity = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")],
        default="medium",
        required=True,
    )
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending_approval", "Pending Approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("applied", "Applied"),
            ("failed", "Failed"),
        ],
        default="draft",
        required=True,
    )
    apply_log = fields.Text()
    error_trace_masked = fields.Text()

    def action_submit_for_approval(self):
        self.write({"status": "pending_approval"})

    def action_approve(self):
        if not self.env.user.has_group("prema_ai_auditor.group_prema_ai_master"):
            raise UserError("Only Prema AI Master Control users can approve proposals.")
        self.write({"status": "approved"})

    def action_reject(self):
        self.write({"status": "rejected"})

    def action_apply(self):
        for proposal in self:
            if proposal.status != "approved":
                raise UserError("Only approved proposals can be applied.")
            if proposal.action_type == "write":
                target = self.env[proposal.target_model].browse(proposal.target_res_id)
                if not target.exists():
                    proposal.write({"status": "failed", "error_trace_masked": "Target record does not exist."})
                    continue
                target.write(proposal.payload_json)
                proposal.write({"status": "applied", "apply_log": "Write proposal applied successfully."})
                continue

            if proposal.action_type == "create":
                created = self.env[proposal.target_model].create(proposal.payload_json)
                proposal.write({"status": "applied", "apply_log": f"Create proposal applied successfully (ID {created.id})."})
                continue

            raise UserError("Unsupported proposal action type.")
