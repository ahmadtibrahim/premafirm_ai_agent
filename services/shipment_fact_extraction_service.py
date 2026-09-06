"""
Structured shipment-fact extraction (MP1 E-A2, master instruction §5).

Generalizes what the invoice / bill-scan services do for their own documents
into a SHARED structured extractor for customer shipment narratives, usable
from FILES (any format the zero-cost document extractor understands — PDF text
layer or OCR, scanned image, Excel, CSV, plain text) and from PASTED TEXT.

Every extracted value carries its provenance:

    row = {
        "field":      canonical field name (shared vocabulary below),
        "value":      normalized value ('' when unknown — never a fact),
        "source":     human-readable label of the document the value came from,
        "kind":       lead_description | attachment | inbound_email
                      (see services/lead_fact_service.py SOURCE_KIND_RANK),
        "at":         document timestamp (datetime or ISO string),
        "confidence": high | medium | low,
        "conflict":   True when another extracted row states a DIFFERENT value
                      for the same field (kept for human review — conflicts are
                      surfaced, never silently auto-resolved here).
    }

HARD SAFETY GUARANTEES (master instruction §5): this service only reads text
and calls the LLM — it never creates a booking, never creates or confirms a
rate confirmation, never sends mail, never writes to the database.  Callers
decide what (if anything) to persist; the MP1 E-A2 contract requires a human
review step before anything is priced, quoted or sent.
"""

import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

# ── Shared shipment-fact vocabulary (field -> instructions for the LLM) ────
# Engine-local canonical keys.  The dispatch-side companion maps these onto
# logistics.custom.quote / logistics.booking fields (docs/A2_CROSS_MODULE_CONTRACT.md).
VOCAB = {
    "reference": "PO/reference number the customer quotes for this shipment",
    "commodity": "what is being shipped (goods description, e.g. 'retail store supplies')",
    "equipment": "requested truck/equipment type (e.g. '53 ft dry van', 'reefer', 'flatbed')",
    "temperature_mode": "temperature requirement as the customer words it (e.g. 'reefer 15C', 'frozen', 'ambient/dry')",
    "temperature_setpoint": "exact temperature setpoint if given in C or F (keep the number and unit, e.g. '15C' or '35F')",
    "package_type": "how the load is packed (e.g. 'pallets', 'skids', 'loose boxes', 'drums')",
    "pallets": "number of pallets/skids (plain number)",
    "cases": "number of cases if stated (plain number)",
    "pieces": "number of pieces if stated (plain number)",
    "weight_lbs": "total weight with unit when the customer states it (e.g. '22000 lbs', '9000 kg'); a bare number is taken as pounds",
    "dimensions": "overall dimensions if stated (e.g. '48x40x60 in')",
    "pickup_date": "pickup DATE as stated, ISO YYYY-MM-DD",
    "pickup_earliest": "earliest pickup TIME as stated, 24-hour HH:MM (e.g. '10:30')",
    "pickup_latest": "latest pickup TIME as stated, 24-hour HH:MM",
    "delivery_date": "delivery DATE as stated, ISO YYYY-MM-DD",
    "delivery_deadline": "delivery deadline TIME as stated, 24-hour HH:MM (customer-required arrival time)",
    "service_minutes": "unload/service time at delivery if stated, in minutes (e.g. 60)",
    "origin_address": "full pickup street address as stated",
    "origin_city": "pickup city as stated",
    "origin_postal_code": "pickup postal code / ZIP as stated",
    "destination_address": "full delivery street address as stated",
    "destination_city": "delivery city as stated",
    "destination_postal_code": "delivery postal code / ZIP as stated",
    "stops": "ADDITIONAL stops beyond origin and destination, as one compact JSON array "
             "[{order: 2, type: 'pickup'|'delivery', address: ..., window: ...}]",
    "accessorials": "special services requested (liftgate, inside delivery, appointment, etc.)",
    "contacts": "customer contact names/roles relevant to the shipment (shipper, receiver, coordinator)",
    "instructions": "operational instructions the customer gives (dock info, appointment rules, flags)",
    "document_number": "number of the quoted document/rate sheet when the customer references one",
}

# Fields whose value should never be invented or converted: keep as stated.
# (Dates/times may be formatted to ISO by the LLM per the instructions above.)
ALLOWED_FIELDS = set(VOCAB.keys())
CONFIDENCES = {"high", "medium", "low"}


def _at_to_iso(at):
    """Serialize a fact timestamp for storage; None stays None."""
    if at is None:
        return None
    if isinstance(at, datetime):
        if at.tzinfo is not None:
            from pytz import UTC
            at = at.astimezone(UTC).replace(tzinfo=None)
        return at.isoformat(sep=" ", timespec="seconds")
    return str(at)


def sanitize_rows(raw_rows, source_label, kind, at):
    """Validate/normalize LLM-produced rows into fact rows.

    Returns ``(rows, warnings)``: unknown fields and unusable values are
    dropped with a warning; they never become facts.
    """
    from odoo.addons.premafirm_ai_engine.services.lead_fact_service import normalize_value  # noqa: E501

    rows, warnings = [], []
    for item in raw_rows or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip().lower()
        if field not in ALLOWED_FIELDS:
            if field:
                warnings.append("Ignored field not in shared vocabulary: %r" % field)
            continue
        value = normalize_value(item.get("value"))
        if not value:
            continue  # unknown/empty values are not facts
        confidence = str(item.get("confidence") or "").strip().lower()
        if confidence not in CONFIDENCES:
            confidence = "low"
        rows.append({
            "field": field,
            "value": value,
            "source": source_label,
            "kind": kind,
            "at": _at_to_iso(at),
            "confidence": confidence,
        })
    return rows, warnings


def flag_conflicts(rows):
    """Mark rows that disagree with another row for the same field.

    Pure function; returns the same list of dicts (mutated copies) with
    ``conflict=True`` where the field carries more than one distinct value.
    Conflicts are flagged for human review, never auto-resolved.
    """
    distinct = {}
    for row in rows or []:
        field = (row.get("field") or "").lower()
        value = (row.get("value") or "").strip()
        if not field or not value:
            continue
        distinct.setdefault(field, set()).add(value)
    conflicted = {f for f, values in distinct.items() if len(values) > 1}
    out = []
    for row in rows or []:
        r = dict(row)
        r["conflict"] = (r.get("field") or "").lower() in conflicted
        out.append(r)
    return out


def merge_rows(rows):
    """Drop exact duplicate (field, value, source, at) rows — first one wins.

    Rows that merely share a field but differ in value are KEPT (they are
    conflicts and must reach the human reviewer / supersession orderer).
    """
    seen, out = set(), []
    for row in rows or []:
        key = (row.get("field"), row.get("value"), row.get("source"), row.get("at"))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


class ShipmentFactExtractionService:
    """Structured extraction of shipment facts from text, files and threads.

    Pure read/AI service — never creates bookings, never confirms or sends
    anything, never writes.  See module docstring for the row contract.
    """

    def __init__(self, env):
        self.env = env

    # ── text ───────────────────────────────────────────────────────────────

    def extract_from_text(self, text, source_label="", kind="lead_description",
                          at=None, max_chars=20000):
        """Extract shipment-fact rows from a text blob.

        Returns ``{"rows": [...], "warnings": [...], "used_chars": n}``.
        ``kind`` is one of lead_description|attachment|inbound_email and feeds
        the supersession tie-break (see lead_fact_service).
        """
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import (  # noqa: E501
            deepseek_chat, get_api_key, get_model, today_context_line,
        )

        warnings = []
        if not text or not str(text).strip():
            return {"rows": [], "warnings": ["No text to extract from."], "used_chars": 0}
        text = str(text).strip()[:max_chars]
        api_key = get_api_key(self.env)
        if not api_key:
            return {"rows": [], "warnings": ["No DeepSeek API key configured — "
                                             "extraction skipped."], "used_chars": len(text)}
        fields = "\n".join(
            "- %s: %s" % (name, doc) for name, doc in sorted(VOCAB.items())
        )
        system = (
            "You extract SHIPMENT FACTS from customer logistics messages for a "
            "transportation company. Never invent a fact: if something is not "
            "stated, leave it out. Do NOT guess prices, rates or quotes. Do NOT "
            "infer delivery deadlines from drive times — only use explicit "
            "customer statements. When the customer CORRECTS an earlier "
            "statement, output the corrected value (the message text you "
            "receive is the newest source).\n"
            "Today is %s.\n"
            "Shared vocabulary (field: what to capture):\n%s\n"
            "Rules:\n"
            "- Dates: ISO YYYY-MM-DD. Times: 24-hour HH:MM exactly as stated "
            "('10:30 AM' -> '10:30', '4:00 p.m.' -> '16:00'). 'before 4:00pm' "
            "is a delivery_deadline of '16:00'.\n"
            "- Times or dates that can only be inferred (never stated) are "
            "omitted.\n"
            "- If the text contradicts itself for one field, emit one row per "
            "stated value so the reviewer sees the conflict.\n"
            "- confidence: 'high' when quoted verbatim, 'medium' when clearly "
            "restated, 'low' when you had to interpret.\n"
            "- Return ONLY a JSON array of {\"field\", \"value\", "
            "\"confidence\", \"note\"} objects. No markdown, no prose."
        )
        try:
            payload = deepseek_chat(
                [{"role": "user", "content": text}],
                system=system,
                model=get_model(self.env),
                api_key=api_key,
                max_tokens=1400,
                timeout=90,
            )
        except Exception as e:  # extraction must never take the flow down
            _logger.warning("shipment-fact extraction AI call failed: %s", e)
            return {"rows": [], "warnings": ["AI extraction failed: %s" % e],
                    "used_chars": len(text)}
        raw_rows = self._parse_json_array(payload)
        if raw_rows is None:
            return {"rows": [], "warnings": ["AI returned no parseable JSON array."],
                    "used_chars": len(text)}
        rows, extra = sanitize_rows(raw_rows, source_label, kind, at)
        warnings.extend(extra)
        return {"rows": flag_conflicts(rows), "warnings": warnings,
                "used_chars": len(text)}

    def _parse_json_array(self, payload):
        """Best-effort JSON-array parse of an LLM payload (mirrors the
        invoice service's tolerant approach)."""
        import json
        import re

        try:
            if isinstance(payload, (list, tuple)):
                return payload
            text = payload.get("content", "") if isinstance(payload, dict) else str(payload)
        except Exception:
            text = str(payload)
        text = (text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else (data.get("rows") or data.get("facts") if isinstance(data, dict) else None)
        except (TypeError, ValueError):
            pass
        # Fence the first [...] block
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, list) else None
        except (TypeError, ValueError):
            return None

    # ── files (and pasted-text-as-file) ────────────────────────────────────

    def extract_from_attachment(self, b64_data, mimetype="", filename="",
                                source_label=None, at=None):
        """Extract shipment facts from ONE file (base64) or pasted text.

        File formats go through the shared zero-cost document extractor
        (PDF text layer -> OCR fallback, image OCR, Excel, CSV, plain text).
        Pasted text is detected when ``filename``/``mimetype`` are empty and
        ``b64_data`` looks like plain text.

        Returns the same ``{"rows", "warnings", ...}`` contract; rows carry
        kind='attachment'.
        """
        from odoo.addons.premafirm_ai_engine.services.document_extractor import (  # noqa: E501
            extract_from_b64,
        )

        source_label = source_label or (filename or "Attachment")
        text, method = extract_from_b64(b64_data, mimetype, filename)
        if method == "none" and filename and b64_data:
            # A name we cannot decode — try as pasted text before giving up.
            try:
                import base64
                raw = base64.b64decode(b64_data) if isinstance(b64_data, str) else b64_data
                text = raw.decode("utf-8", errors="ignore")
                method = "plain" if len(text.strip()) > 20 else "none"
            except Exception:
                method = "none"
        if method == "none":
            return {"rows": [], "warnings": ["Could not read any text from %s "
                                             "(method=none)." % source_label],
                    "method": "none"}
        result = self.extract_from_text(
            text, source_label=source_label, kind="attachment", at=at,
        )
        result["method"] = method
        result["file"] = source_label
        return result

    def extract_from_attachments(self, attachments):
        """Loop helper over ir.attachment-like records (``datas``, ``mimetype``,
        ``name``).  Returns ``{"rows", "warnings", "files": [...]}``."""
        rows, warnings, files = [], [], []
        for att in attachments:
            result = self.extract_from_attachment(
                att.datas, att.mimetype, att.name,
                source_label=att.name or "Attachment", at=None,
            )
            files.append({"name": att.name or "?", "method": result.get("method")})
            rows.extend(result.get("rows") or [])
            warnings.extend(result.get("warnings") or [])
        rows = flag_conflicts(rows)
        return {"rows": rows, "warnings": warnings, "files": files}
