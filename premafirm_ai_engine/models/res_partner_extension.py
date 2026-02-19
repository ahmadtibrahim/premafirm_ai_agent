from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    is_driver = fields.Boolean(default=False)


    def init(self):
        """Backfill schema drift when the module code is deployed before `-u`.

        Some environments load this field in the registry before the module
        upgrade has created its SQL column, which crashes any partner fetch.
        Keep startup resilient by creating the column if it is still missing.

        """
        self.env.cr.execute(
            "ALTER TABLE res_partner "
            "ADD COLUMN IF NOT EXISTS is_driver boolean DEFAULT false"
        )
   main
