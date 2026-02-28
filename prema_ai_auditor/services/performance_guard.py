from odoo import fields, models
from odoo.exceptions import UserError


class PremaPerformanceGuard(models.AbstractModel):
    _name = "prema.performance.guard"
    _description = "Prema Performance Guard"

    def assert_rate_limit(self, key, max_calls=20, window_seconds=60):
        now = fields.Datetime.now()
        window_start = fields.Datetime.subtract(now, seconds=window_seconds)
        count = self.env["prema.ai.audit.log"].search_count(
            [
                ("action", "=", f"llm_call:{key}"),
                ("create_date", ">=", window_start),
            ]
        )
        if count >= max_calls:
            raise UserError("Rate limit exceeded for LLM calls.")
