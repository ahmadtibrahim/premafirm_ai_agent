import logging
import time

import requests

_logger = logging.getLogger(__name__)

SNOV_BASE = "https://api.snov.io/v1"
SNOV_BASE_V2 = "https://api.snov.io/v2"

CARRIER_PROSPECT_TITLES = [
    "VP of Logistics",
    "VP Supply Chain",
    "Director of Logistics",
    "Director of Supply Chain",
    "Logistics Manager",
    "Supply Chain Manager",
    "Transportation Manager",
    "Dispatch Manager",
    "Dispatch Supervisor",
    "Logistics Coordinator",
    "Transportation Coordinator",
    "Procurement Manager",
    "Purchasing Manager",
    "Operations Manager",
    "Warehouse Manager",
    "Distribution Manager",
    "Traffic Manager",
    "Freight Manager",
    "Shipping Manager",
    "Fleet Manager",
    "Import Export Manager",
    "Inventory Manager",
]


class SnovService:
    """Snov.io API client (OAuth2 client_credentials).

    find_emails_by_domain  — finds contacts at a company domain (low credit cost)
    find_email_by_person   — finds email for a named person (spends credits)
    verify_email           — checks deliverability (spends credits)
    """

    def __init__(self, env):
        self.env = env
        self._token = None
        self._token_expires = 0.0

    def _get_credentials(self):
        ICP = self.env["ir.config_parameter"].sudo()
        client_id = ICP.get_param("snov.client_id")
        client_secret = ICP.get_param("snov.client_secret")
        if not client_id or not client_secret:
            raise ValueError(
                "Snov.io credentials not configured. Go to Settings → Outreach / Snov.io to add them."
            )
        return client_id, client_secret

    def authenticate(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        client_id, client_secret = self._get_credentials()
        try:
            resp = requests.post(
                f"{SNOV_BASE}/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires = time.time() + data.get("expires_in", 3600)
            return self._token
        except requests.HTTPError as e:
            _logger.error("Snov.io auth failed %s", e)
            raise ValueError(f"Snov.io authentication failed: {e.response.status_code}")
        except requests.RequestException as e:
            _logger.exception("Snov.io auth connection error")
            raise ValueError(f"Snov.io connection error: {e}")

    def test_connection(self):
        """Returns True on success, error string on failure."""
        try:
            self.authenticate()
            return True
        except ValueError as e:
            return str(e)

    def find_emails_by_domain(self, domain, positions=None, limit=10, last_id=0):
        """Find emails registered at a domain. Optionally filter by job positions.

        positions: list of title strings or comma-separated string.
        Returns Snov.io domain email response dict.
        """
        token = self.authenticate()
        # Snov.io expects repeated keys for list params; use a list of tuples
        payload = [
            ("access_token", token),
            ("domain", domain),
            ("type", "all"),
            ("limit", limit),
            ("lastId", last_id),
        ]
        if positions:
            if isinstance(positions, str):
                positions = [p.strip() for p in positions.split(",") if p.strip()]
            for pos in positions:
                payload.append(("positions[]", pos))

        try:
            resp = requests.post(f"{SNOV_BASE}/get-domain-emails-with-info", data=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                # Domain not indexed in Snov.io — treat as empty result, not an error
                _logger.info("Snov.io: domain %s not found (404)", payload[1][1] if len(payload) > 1 else "unknown")
                return {"emails": []}
            body = ""
            try:
                body = e.response.json()
            except Exception:
                pass
            _logger.error("Snov.io domain search HTTP %s: %s", e.response.status_code, body)
            raise ValueError(f"Snov.io returned {e.response.status_code}: {body}")
        except requests.RequestException as e:
            _logger.exception("Snov.io domain search request failed")
            raise ValueError(f"Snov.io connection error: {e}")

    def find_email_by_person(self, first_name, last_name, domain):
        """Two-step: queue the name lookup, then retrieve the result.

        Snov.io processes asynchronously — result may be empty on first call.
        Returns Snov.io response dict with 'emails' list.
        """
        token = self.authenticate()
        try:
            requests.post(
                f"{SNOV_BASE}/add-names-to-find-emails",
                data={
                    "access_token": token,
                    "firstName": first_name,
                    "lastName": last_name,
                    "domain": domain,
                },
                timeout=15,
            )
            resp = requests.get(
                f"{SNOV_BASE}/get-emails-from-names",
                params={
                    "access_token": token,
                    "firstName": first_name,
                    "lastName": last_name,
                    "domain": domain,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            _logger.error("Snov.io name lookup HTTP %s", e.response.status_code)
            raise ValueError(f"Snov.io returned {e.response.status_code}")
        except requests.RequestException as e:
            _logger.exception("Snov.io name lookup failed")
            raise ValueError(f"Snov.io connection error: {e}")

    def verify_email(self, email):
        """Queue email verification and retrieve result.

        Returns a dict with 'result' key: valid / invalid / unknown / catch-all.
        """
        token = self.authenticate()
        try:
            requests.post(
                f"{SNOV_BASE}/add-emails-to-verification-queue",
                data={"access_token": token, "emails[]": email},
                timeout=15,
            )
            resp = requests.get(
                f"{SNOV_BASE}/get-email-verification-result",
                params={"access_token": token, "emails[]": email},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return data
        except requests.HTTPError as e:
            _logger.error("Snov.io verify HTTP %s", e.response.status_code)
            raise ValueError(f"Snov.io returned {e.response.status_code}")
        except requests.RequestException as e:
            _logger.exception("Snov.io verify failed")
            raise ValueError(f"Snov.io connection error: {e}")

    def get_company_info(self, domain):
        """Get company profile by domain from Snov.io.

        Endpoint: GET /v2/company-info-by-domain
        Returns a dict with company data (name, website, industry, companySize,
        country, locality, phone, linkedin, description, etc.), or empty dict.
        """
        token = self.authenticate()
        try:
            resp = requests.get(
                f"{SNOV_BASE_V2}/company-info-by-domain",
                params={"access_token": token, "domain": domain},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return {}

            # Standard v2 envelope: {"success": true, "data": {...}}
            if data.get("success") and data.get("data") and isinstance(data["data"], dict):
                inner = data["data"]
                if inner.get("name") or inner.get("website") or inner.get("phone"):
                    return inner

            # Some endpoints return the company object directly at root level
            if data.get("name") or data.get("website") or data.get("phone"):
                return data

            # Success=true but empty/null data (domain not in Snov.io DB)
            if data.get("success") is True:
                return {}

            # Error responses: {"success": false, "message": "..."}
            if data.get("success") is False:
                msg = data.get("message") or data.get("error") or "no data"
                _logger.info("Snov.io company-info-by-domain: %s for domain %s", msg, domain)
                return {}

            return {}
        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 404:
                # Domain not in Snov.io database — not an error, just no data
                _logger.info("Snov.io: domain %s not found (404)", domain)
                return {}
            body = ""
            try:
                body = e.response.json()
            except Exception:
                pass
            _logger.error("Snov.io company info HTTP %s: %s", status, body)
            raise ValueError(f"Snov.io returned {status}: {body}")
        except requests.RequestException as e:
            _logger.exception("Snov.io company info request failed")
            raise ValueError(f"Snov.io connection error: {e}")

    def get_carrier_prospect_titles(self):
        return CARRIER_PROSPECT_TITLES
