from odoo import models


class FXValidator(models.AbstractModel):
    _name = "prema.fx.validator"
    _description = "FX Variance Validator"

    def validate_fx_variance(self):
        moves = self.env["account.move"].search(
            [("currency_id", "!=", False), ("state", "=", "posted")]
        )

        for move in moves:
            if abs(move.amount_total - move.amount_total_signed) > 5:
                self.env["prema.audit.log"].create(
                    {
                        "rule_name": "FX Variance",
                        "severity": "medium",
                        "model_name": "account.move",
                        "record_id": move.id,
                        "explanation": "Foreign exchange difference detected",
                    }
                )
