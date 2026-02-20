import json
from pathlib import Path


class DispatchRulesEngine:
    """Singleton-backed helper for dispatch_rules.json access."""

    _cache = None

    def __init__(self, env=None):
        self.env = env
        DispatchRulesEngine._ensure_cache()

    @classmethod
    def _load_rules(cls):
        rules_path = Path(__file__).resolve().parents[1] / "data" / "dispatch_rules.json"
        with rules_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @classmethod
    def _ensure_cache(cls):
        if cls._cache is None:
            cls._cache = cls._load_rules()

    @classmethod
    def _rules_dict(cls):
        cls._ensure_cache()
        return cls._cache or {}

    @property
    def rules(self):
        return self._rules_dict()

    def get(self, section, default=None):
        return self.rules.get(section, default if default is not None else {})

    def select_product(self, customer_country, structure, equipment):
        product_engine = self.get("product_selection_engine", {})
        mapping = product_engine.get("mapping_by_customer_country", {})
        country_key = "USA" if (customer_country or "").strip().upper() in {"US", "USA", "UNITED STATES"} else "Canada"
        country_map = mapping.get(country_key) or mapping.get("Canada", {})
        structure_map = country_map.get(structure.upper(), {})
        equipment_key = "Reefer" if (equipment or "").strip().lower() == "reefer" else "Dry"
        return structure_map.get(equipment_key)

    @classmethod
    def accessorial_product_ids(cls, liftgate=False, inside_delivery=False):
        accessorials = cls._rules_dict().get("product_selection_engine", {}).get("accessorials", {})
        products = []
        if liftgate:
            product_id = accessorials.get("Liftgate")
            if product_id:
                products.append(product_id)
        if inside_delivery:
            product_id = accessorials.get("Inside_Delivery")
            if product_id:
                products.append(product_id)
        return products
