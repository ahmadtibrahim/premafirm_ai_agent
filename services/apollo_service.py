import logging

import requests

_logger = logging.getLogger(__name__)

APOLLO_BASE = "https://api.apollo.io/v1"

# Titles that buy transportation/freight services — relevant for a carrier prospecting
CARRIER_PROSPECT_TITLES = [
    "Logistics Manager",
    "Supply Chain Manager",
    "Transportation Manager",
    "Procurement Manager",
    "Purchasing Manager",
    "Operations Manager",
    "Director of Logistics",
    "Director of Supply Chain",
    "VP of Logistics",
    "VP Supply Chain",
    "Warehouse Manager",
    "Distribution Manager",
    "Traffic Manager",
    "Freight Manager",
    "Shipping Manager",
    "Fleet Manager",
    "Import Export Manager",
    "Inventory Manager",
]


class ApolloService:
    """Apollo.io API client.

    search_people  — uses mixed_people/search (low credit cost, safe for broad queries)
    enrich_person  — uses people/match (spends credits, call only on explicit user action)
    """

    def __init__(self, env):
        self.env = env

    def _get_api_key(self):
        key = self.env["ir.config_parameter"].sudo().get_param("apollo.api_key")
        if not key:
            raise ValueError("Apollo API key is not configured. Go to Settings → Outreach / Apollo to add it.")
        return key

    def _headers(self):
        return {
            "X-Api-Key": self._get_api_key(),
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

    def get_carrier_prospect_titles(self):
        return CARRIER_PROSPECT_TITLES

    def search_people(self, filters: dict, page: int = 1, per_page: int = 25) -> dict:
        """Search for prospect contacts. Low / no credit cost depending on plan."""
        payload = {"page": page, "per_page": per_page}

        if filters.get("company_name"):
            payload["q_organization_name"] = filters["company_name"]
        if filters.get("job_titles"):
            titles = filters["job_titles"]
            payload["person_titles"] = titles if isinstance(titles, list) else [t.strip() for t in titles.split(",") if t.strip()]
        if filters.get("city"):
            payload["person_locations"] = [filters["city"]]
        if filters.get("country"):
            payload["person_locations"] = payload.get("person_locations", []) + [filters["country"]]
        if filters.get("keywords"):
            payload["q_keywords"] = filters["keywords"]

        try:
            resp = requests.post(
                f"{APOLLO_BASE}/mixed_people/search",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.json()
            except Exception:
                pass
            _logger.error("Apollo search HTTP error %s: %s", e.response.status_code, body)
            raise ValueError(f"Apollo API returned {e.response.status_code}: {body}")
        except requests.RequestException as e:
            _logger.exception("Apollo search request failed")
            raise ValueError(f"Apollo API connection error: {e}")

    def enrich_person(self, email: str = None, first_name: str = None,
                      last_name: str = None, organization_name: str = None) -> dict:
        """Enrich a single person. USES CREDITS — only call on explicit user click."""
        payload = {"reveal_personal_emails": False}
        if email:
            payload["email"] = email
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if organization_name:
            payload["organization_name"] = organization_name

        try:
            resp = requests.post(
                f"{APOLLO_BASE}/people/match",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.json()
            except Exception:
                pass
            _logger.error("Apollo enrich HTTP error %s: %s", e.response.status_code, body)
            raise ValueError(f"Apollo enrich returned {e.response.status_code}: {body}")
        except requests.RequestException as e:
            _logger.exception("Apollo enrich request failed")
            raise ValueError(f"Apollo enrich connection error: {e}")
