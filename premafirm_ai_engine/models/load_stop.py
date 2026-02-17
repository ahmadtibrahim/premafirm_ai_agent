from odoo import api, fields, models


class PremafirmLoadStop(models.Model):
    _name = "premafirm.load.stop"
    _description = "Premafirm Load Stop"
    _order = "sequence asc, id asc"

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    is_pickup = fields.Boolean(default=True)
    address = fields.Char(required=True)
    latitude = fields.Float()
    longitude = fields.Float()
    stop_pickup_dt = fields.Datetime()
    stop_delivery_dt = fields.Datetime()
    pallet_count = fields.Integer()
    load_weight_lbs = fields.Integer()
    service_type = fields.Selection([("reefer", "Reefer"), ("dry", "Dry")])
    load_type = fields.Selection([("FTL", "FTL"), ("LTL", "LTL")])
    notes = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("sequence"):
                lead_id = vals.get("lead_id")
                if lead_id:
                    last = self.search([("lead_id", "=", lead_id)], order="sequence desc", limit=1)
                    vals["sequence"] = (last.sequence or 0) + 10
                else:
                    vals["sequence"] = 10
        return super().create(vals_list)
