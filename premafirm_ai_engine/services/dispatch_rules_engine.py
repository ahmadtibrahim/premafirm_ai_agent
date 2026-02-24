import json
from pathlib import Path

from odoo.exceptions import UserError


class DispatchRulesEngine:
    """Singleton-backed helper for dispatch_rules.json access."""

    _cache = None

    def __init__(self, env=None):
        self.env = env
        if DispatchRulesEngine._cache is None:
            DispatchRulesEngine._cache = self._load_rules()

    @classmethod
    def _load_rules(cls):
        rules_path = Path(__file__).resolve().parents[1] / "data" / "dispatch_rules.json"
        with rules_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @property
    def rules(self):
        return self._cache or {}

    @classmethod
    def _rules(cls):
        if cls._cache is None:
            cls._cache = cls._load_rules()
        return cls._cache or {}

    def get(self, section, default=None):
        return self.rules.get(section, default if default is not None else {})

    def select_product(self, customer_country, structure, equipment):
        country = (customer_country or "").strip().upper()
        if country in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
            country_code = "US"
        else:
            country_code = "CA"

        load_type = (structure or "").strip().upper()
        is_reefer = (equipment or "").strip().lower() == "reefer"
        return self._resolve_freight_product(country_code, load_type, is_reefer).id

    def _resolve_freight_product(self, country_code, load_type, is_reefer):
        template_map = {
            "CA": {
                "FTL": "FTL Freight Service - Canada",
                "LTL": "LTL Freight Service - Canada",
            },
            "US": {
                "FTL": "FTL - Freight Service - USA",
                "LTL": "LTL - Freight Service - USA",
            },
        }

        if country_code not in template_map:
            raise UserError(f"Unsupported country: {country_code}")

        if load_type not in ["FTL", "LTL"]:
            raise UserError(f"Invalid load type: {load_type}")

        template_name = template_map[country_code][load_type]

        template = self.env["product.template"].sudo().search(
            [("name", "=", template_name)],
            limit=1,
        )

        if not template:
            raise UserError(f"Freight template not found: {template_name}")

        if load_type == "FTL":
            attribute_name = "Reefer FTL" if is_reefer else "FTL"
        else:
            attribute_name = "Reefer LTL" if is_reefer else "LTL"

        variant = template.product_variant_ids.filtered(
            lambda v: attribute_name in v.product_template_attribute_value_ids.mapped("name")
        )

        if not variant:
            raise UserError(
                f"Freight variant not found for template {template_name} with attribute {attribute_name}"
            )

        product = variant[0]

        if not product.active:
            raise UserError(f"Freight product archived: {product.display_name}")

        return product

    def _resolve_accessorial(self, name):
        template = self.env["product.template"].sudo().search(
            [("name", "=", name)],
            limit=1,
        )

        if not template:
            raise UserError(f"Accessorial not found: {name}")

        product = template.product_variant_id

        if not product.active:
            raise UserError(f"Accessorial archived: {name}")

        return product

    def accessorial_product_ids(self, liftgate=False, inside_delivery=False):
        products = []
        if liftgate:
            products.append(self._resolve_accessorial("Liftgate").id)
        if inside_delivery:
            products.append(self._resolve_accessorial("Inside Delivery").id)
        return products
