"""
Lead shipment-fact service (MP1 E-A2, master instruction §3.2).

Turns the raw customer narrative of a CRM opportunity — the lead description,
the inbound customer email thread, and later explicit customer corrections —
into a small set of EFFECTIVE shipment facts where newer, explicit customer
statements supersede older ones and every surviving value stays visibly
sourced (who said it, when, on which document).

Design rules
------------
* Supersession is decided on EXTRACTED facts, not on raw text: each document
  is first turned into candidate facts by a structured extractor (see
  services/shipment_fact_extraction_service.py), then this module orders the
  candidates per field.
* Per field the NEWEST customer-provided value wins. "Newest" is measured by
  the document timestamp (``at``); when two candidates carry the same
  timestamp the source kind breaks the tie — an explicit customer email beats
  an attachment, which beats the original lead description. Values that parse
  as empty/unknown never supersede a concrete value.
* Staff-authored chatter is NOT a shipment-fact source (an internal guess must
  never silently become a shipment fact) — the same rule the dispatch-side
  lead bridge already applies (``_dispatch_rate_source_text``).
* The ordering logic is a pure, DB-free function (``resolve_effective_facts``)
  so it is directly unit-testable with synthetic fixtures.

The service layer (``LeadFactService``) only gathers documents and wires the
extractor + ordering together. It never writes to the database, never prices,
never emails anything.
"""

import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

# ── Source kinds and their tie-break authority (ascending) ────────────────
# A LATER timestamp always wins regardless of kind; the rank only decides
# between candidates with EQUAL timestamps.
SOURCE_KIND_RANK = {
    "lead_description": 1,   # the opportunity description itself (oldest anchor)
    "attachment": 2,         # a document the customer provided
    "inbound_email": 3,      # explicit customer email — incl. later corrections
}

DEFAULT_KIND = "lead_description"

# Values that carry no information and must never supersede a concrete fact.
# (Includes no-information markers an LLM may emit despite instructions.)
_EMPTY_VALUES = {"", "none", "null", "nil", "n/a", "na", "tbd", "unknown",
                 "not stated", "not specified", "unspecified", "not provided",
                 "not found", "never extracted", "-"}


def normalize_value(value):
    """Strip and canonicalize an extracted value; '' when it is empty/unknown."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _EMPTY_VALUES:
        return ""
    return text


def _candidate_time(fact):
    """Return a comparable timestamp (seconds since epoch; 0 when unknown)."""
    at = fact.get("at")
    if not at:
        return 0
    if isinstance(at, (int, float)):
        return float(at)
    if isinstance(at, datetime):
        dt = at
    else:  # ISO-ish string
        try:
            dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            try:
                dt = datetime.strptime(str(at)[:19], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=__import__("pytz").utc)
    return dt.timestamp()


def _kind_rank(fact):
    return SOURCE_KIND_RANK.get(fact.get("kind") or DEFAULT_KIND, 0)


def _beats(new, current):
    """True when candidate ``new`` supersedes the current effective fact."""
    new_time = _candidate_time(new)
    cur_time = _candidate_time(current)
    if new_time != cur_time:
        return new_time > cur_time
    # Same timestamp: a more authoritative source kind wins; a fully-equal
    # candidate (same source + time) is a duplicate and does not beat.
    return _kind_rank(new) > _kind_rank(current)


def resolve_effective_facts(candidates):
    """Per-field supersession over extracted fact candidates.

    ``candidates`` — iterable of dicts with keys::

        field       str   canonical field name (shared vocabulary)
        value       str   extracted value (may be empty/unknown)
        source      str   human-readable provenance label (document name)
        kind        str   one of SOURCE_KIND_RANK (default lead_description)
        at          datetime/str/None  document timestamp
        confidence  str   high|medium|low (optional, carried through)

    Returns ``{"effective": {...}, "superseded": [...]}``:

    * ``effective`` maps field -> the winning candidate dict (empty values are
      dropped first, so a later 'unknown' never erases a real value);
    * ``superseded`` lists every candidate that lost to a newer/more
      authoritative one, so callers can surface what changed and why.
    """
    effective = {}
    superseded = []
    by_field = {}
    for fact in candidates or []:
        field = (fact.get("field") or "").strip()
        value = normalize_value(fact.get("value"))
        if not field or not value:
            continue  # unknown values never become (or replace) facts
        key = field.lower()
        by_field.setdefault(key, []).append(fact)

    for field, facts in by_field.items():
        winner = None
        for fact in facts:  # deterministic order: caller order, then beats()
            if winner is None or _beats(fact, winner):
                if winner is not None:
                    superseded.append(winner)
                winner = fact
        effective[field] = dict(winner)
    return {"effective": effective, "superseded": superseded}


class LeadFactService:
    """Gather a lead's customer documents and reduce them to effective facts.

    Pure orchestration over ORM reads — never writes, never prices, never
    sends anything.  The dispatch-side companion (E-A2 contract) calls this
    before pricing or before drafting a Rate Confirmation.
    """

    def __init__(self, env):
        self.env = env

    # ── document gathering ────────────────────────────────────────────────

    def _inbound_customer_messages(self, lead, limit=30):
        """Inbound emails from the customer side of the thread, oldest first.

        Staff-authored messages are excluded: an internal guess must never
        silently become a shipment fact (same rule as the dispatch lead
        bridge).  Returns mail.message records sorted by date ascending.
        """
        self.ensure_lead(lead)
        messages = lead.message_ids.filtered(
            lambda m: m.message_type == "email"
            and m.body
            and m.author_id
            # Staff authors are counted regardless of their user's active flag
            # (the __system__ account is deactivated on this server; an
            # inactive internal user's messages are still internal).
            and not m.author_id.with_context(active_test=False).user_ids
        )
        return messages.sorted("date")[:limit]

    def collect_documents(self, lead, limit_emails=30):
        """Return the ordered customer documents of a lead as dicts.

        ``[{kind, source, at, text}]`` — the lead description first, then the
        inbound customer emails oldest -> newest, so later corrections come
        last and naturally supersede older statements.
        """
        from odoo.tools import html2plaintext

        self.ensure_lead(lead)
        docs = []
        description = html2plaintext(lead.description or "").strip()
        if description:
            docs.append({
                "kind": "lead_description",
                "source": "Lead description",
                "at": lead.create_date,
                "text": description[:20000],
            })
        for msg in self._inbound_customer_messages(lead, limit_emails):
            body = html2plaintext(msg.body or "").strip()
            if not body:
                continue
            docs.append({
                "kind": "inbound_email",
                "source": "Customer email %s" % (msg.date or "?"),
                "at": msg.date,
                "text": body[:20000],
            })
        return docs

    # ── effective facts ───────────────────────────────────────────────────

    def extract_effective_facts(self, lead, extractor=None, docs=None):
        """Run the per-document extractor over every customer document and
        reduce the candidates with the supersession ordering.

        ``extractor`` — callable(text, source=..., kind=..., at=...) returning
        a dict ``{"rows": [...], "warnings": [...]}`` (same contract as
        ShipmentFactExtractionService.extract_from_text).  Defaults to that
        service.

        Returns ``{"effective": {field: fact}, "rows": [...], "docs": [...],
        "warnings": [...]}`` — effective facts keep full provenance
        (value + source + at + confidence) so they remain visibly sourced.
        """
        from odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service import (  # noqa: E501
            ShipmentFactExtractionService,
        )

        self.ensure_lead(lead)
        if docs is None:
            docs = self.collect_documents(lead)
        if extractor is None:
            extractor = ShipmentFactExtractionService(self.env).extract_from_text
        rows, warnings = [], []
        for doc in docs:
            result = extractor(
                doc["text"],
                source=doc["source"],
                kind=doc["kind"],
                at=doc["at"],
            )
            rows.extend(result.get("rows") or [])
            warnings.extend(result.get("warnings") or [])
        resolved = resolve_effective_facts(rows)
        return {
            "effective": resolved["effective"],
            "superseded": resolved["superseded"],
            "rows": rows,
            "docs": docs,
            "warnings": warnings,
        }

    @staticmethod
    def ensure_lead(lead):
        if not lead or not lead.ids:
            raise ValueError("A CRM opportunity is required to collect shipment facts.")
