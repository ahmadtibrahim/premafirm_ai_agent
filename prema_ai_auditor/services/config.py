import os

from odoo import models
from odoo.exceptions import UserError


class PremaConfigService(models.AbstractModel):
    _name = "prema.config.service"
    _description = "Prema Central Configuration Service"

    def get(self, param_key, env_key=None, default=""):
        value = self.env["ir.config_parameter"].sudo().get_param(param_key)
        if value:
            return value
        if env_key:
            env_value = os.getenv(env_key)
            if env_value:
                return env_value
        return default

    def require(self, param_key, env_key=None, label=None):
        value = self.get(param_key, env_key=env_key, default="")
        if not value:
            raise UserError(f"Missing required configuration: {label or param_key}")
        return value

    @staticmethod
    def mask_secret(value):
        if not value:
            return ""
        if len(value) <= 8:
            return "********"
        return f"{value[:4]}...{value[-4:]}"

