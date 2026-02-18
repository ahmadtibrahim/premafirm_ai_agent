from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")

    load_reference = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        company_currency = self.env.company.currency_id.id
        for vals in vals_list:
            vals.setdefault("currency_id", company_currency)
        return super().create(vals_list)
