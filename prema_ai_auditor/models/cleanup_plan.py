from odoo import fields, models
from odoo.exceptions import UserError


class PremaCleanupPlan(models.Model):
    _name = "prema.cleanup.plan"
    _description = "AI Cleanup Plan"

    name = fields.Char(required=True)
    generated_by_ai = fields.Boolean(default=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("stage_1", "Stage 1 Approved"),
            ("stage_2", "Stage 2 Approved"),
            ("executing", "Executing"),
            ("completed", "Completed"),
        ],
        default="draft",
        required=True,
    )

    step_ids = fields.One2many("prema.cleanup.step", "plan_id")

    risk_score = fields.Integer()
    summary = fields.Text()

    def approve_stage(self):
        stage_number = self.env.context.get("stage_number")
        for plan in self:
            steps = plan.step_ids.filtered(lambda s: s.approval_stage == stage_number)
            steps.write({"approved": True})

            if stage_number == 1:
                plan.status = "stage_1"
            elif stage_number == 2:
                plan.status = "stage_2"

    def execute_plan(self):
        for plan in self:
            if plan.status != "stage_2":
                raise UserError("Plan not fully approved")

            plan.status = "executing"
            for step in plan.step_ids.filtered(lambda s: s.approved and not s.executed):
                self.env["prema.tool.registry"].execute(
                    step.action_type,
                    {"record_id": step.record_id},
                )
                step.executed = True

            plan.status = "completed"
