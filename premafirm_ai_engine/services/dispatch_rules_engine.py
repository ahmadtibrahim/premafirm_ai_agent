import json
from pathlib import Path


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

    def get(self, section, default=None):
        return self.rules.get(section, default if default is not None else {})

    def select_product(self, customer_country, structure, equipment):
        engine = self.get("product_selection_engine", {})
        mapping = engine.get("mapping_by_customer_country", {})

        country_key = "USA" if (customer_country or "").strip().upper() in {"US", "USA", "UNITED STATES"} else "Canada"

        try:
            return mapping[country_key][structure.upper()][equipment.title()]
        except KeyError:
            raise ValueError(f"Product mapping not found for {country_key} / {structure} / {equipment}")

    def accessorial_product_ids(self, liftgate=False, inside_delivery=False):
        engine = self.get("product_selection_engine", {})
        accessorials = engine.get("accessorials", {})

        products = []

        if liftgate:
            products.append(accessorials.get("Liftgate"))

        if inside_delivery:
            products.append(accessorials.get("Inside_Delivery"))

        return [p for p in products if p]
