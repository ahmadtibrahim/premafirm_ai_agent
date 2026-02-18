from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")

    load_reference = fields.Char()
