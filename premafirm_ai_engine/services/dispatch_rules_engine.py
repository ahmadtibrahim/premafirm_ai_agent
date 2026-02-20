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
        mapping = {
            "USA": {
                "FTL": {"Dry": 266, "Reefer": 264},
                "LTL": {"Dry": 278, "Reefer": 276},
            },
            "CANADA": {
                "FTL": {"Dry": 262, "Reefer": 259},
                "LTL": {"Dry": 274, "Reefer": 273},
            },
        }
        country_key = "USA" if (customer_country or "").strip().upper() in {"US", "USA", "UNITED STATES"} else "CANADA"
        return mapping[country_key][structure.upper()][equipment.title()]

    @staticmethod
    def accessorial_product_ids(liftgate=False, inside_delivery=False):
        products = []
        if liftgate:
            products.append(269)
        if inside_delivery:
            products.append(270)
        return products
