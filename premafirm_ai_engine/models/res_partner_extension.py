from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    is_driver = fields.Boolean(default=False)

    def _register_hook(self):
        """Guard against schema drift when code is deployed before module update.

        `_register_hook` executes during every registry load, which lets us keep
        the database resilient even if `-u premafirm_ai_engine` has not been run
        yet in an environment where this field was introduced.
        """
        self.env.cr.execute(
            "ALTER TABLE res_partner "
            "ADD COLUMN IF NOT EXISTS is_driver boolean DEFAULT false"
        )
        return super()._register_hook()
