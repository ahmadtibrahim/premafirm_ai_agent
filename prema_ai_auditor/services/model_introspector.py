from odoo import models
from odoo.tools import ormcache


class ModelIntrospector(models.AbstractModel):
    _name = "prema.model.introspector"
    _description = "Prema Model Introspector"

    @ormcache()
    def get_schema_cached(self):
        return self._build_schema()

    def _build_schema(self):
        model_names = [
            "account.move",
            "account.payment",
            "sale.order",
            "purchase.order",
            "mail.mail",
        ]
        schema = {}
        for model_name in model_names:
            model = self.env[model_name]
            schema[model_name] = {
                "fields": list(model.fields_get().keys())[:80],
            }
        return schema
