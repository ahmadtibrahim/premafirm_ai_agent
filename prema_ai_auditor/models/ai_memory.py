from odoo import fields, models
from odoo.exceptions import UserError


class AIFixMemory(models.Model):
    _name = "prema.ai.memory"
    _description = "Prema AI Fix Memory"
    _order = "usage_count desc, write_date desc"

    issue_signature = fields.Char(required=True, index=True)
    resolution_summary = fields.Text(required=True)
    success_rate = fields.Float(default=1.0)
    usage_count = fields.Integer(default=0)

    _sql_constraints = [
        ("prema_ai_memory_issue_signature_unique", "unique(issue_signature)", "Issue signature must be unique."),
    ]

    @classmethod
    def _validate_approval_token(cls, env, approval_token):
        request = env["prema.ai.action.request"].search(
            [
                ("token", "=", approval_token),
                ("approved", "=", True),
                ("executed", "=", False),
            ],
            limit=1,
        )
        if not request:
            raise UserError("A valid approved token is required.")
        if fields.Datetime.now() > request.expires_at:
            raise UserError("Approval token expired.")
        return request

    @classmethod
    def _compute_success_rate(cls, current_rate, usage_count, success):
        total = current_rate * usage_count
        total += 1.0 if success else 0.0
        return total / (usage_count + 1)

    @classmethod
    def remember_fix(cls, env, approval_token, issue_signature, resolution_summary, success=True):
        request = cls._validate_approval_token(env, approval_token)

        memory = env["prema.ai.memory"].search([("issue_signature", "=", issue_signature)], limit=1)
        if memory:
            new_rate = cls._compute_success_rate(memory.success_rate, memory.usage_count, success)
            memory.write(
                {
                    "resolution_summary": resolution_summary,
                    "usage_count": memory.usage_count + 1,
                    "success_rate": new_rate,
                }
            )
        else:
            memory = env["prema.ai.memory"].create(
                {
                    "issue_signature": issue_signature,
                    "resolution_summary": resolution_summary,
                    "usage_count": 1,
                    "success_rate": 1.0 if success else 0.0,
                }
            )

        request.executed = True
        env["prema.ai.audit.log"].create(
            {
                "action": "memory_record_fix",
                "status": "success",
                "details": "Fix memory updated from approved request.",
                "action_request_id": request.id,
                "model_name": "prema.ai.memory",
                "record_id": memory.id,
            }
        )
        return memory
