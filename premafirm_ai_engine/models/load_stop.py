from odoo import api, fields, models


class PremafirmLoadStop(models.Model):
    _name = "premafirm.load.stop"
    _description = "Premafirm Load Stop"
    _order = "sequence asc, id asc"

    lead_id = fields.Many2one(
        "crm.lead",
        required=True,
        ondelete="cascade"
    )

    sequence = fields.Integer(default=10)

    stop_type = fields.Selection(
        [
            ("pickup", "Pickup"),
            ("delivery", "Delivery"),
        ],
        required=True
    )

    address = fields.Char(required=True)

    pallets = fields.Integer()
    weight_lbs = fields.Float()

    x_studio_pickup_dt = fields.Datetime()
    x_studio_delivery_dt = fields.Datetime()
    x_studio_window_start = fields.Datetime()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("sequence"):
                lead_id = vals.get("lead_id")
                if lead_id:
                    last = self.search(
                        [("lead_id", "=", lead_id)],
                        order="sequence desc",
                        limit=1
                    )
                    vals["sequence"] = (last.sequence or 0) + 10
                else:
                    vals["sequence"] = 10
        return super().create(vals_list)
