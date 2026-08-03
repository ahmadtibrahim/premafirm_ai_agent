import base64
import json
import logging
import re
from datetime import datetime

_logger = logging.getLogger(__name__)


class DispatchDocumentService:
    """Generic low-credit dispatch document parsing.

    Strategy:
      1. Extract text locally whenever possible.
      2. Classify document shape heuristically.
      3. Parse stop/address/time-window structure locally.
      4. Persist structured learnings to PremaFirm ML when available.
      5. Fall back to AI only when local confidence is too low.
    """

    STREET_RE = re.compile(
        r"\b\d{1,6}\s+[A-Za-z0-9#.,'’/\- ]+\b"
        r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Parkway|Pkwy|Highway|Hwy|Route|Rte|Trail|Trl|Circle|Cir|Terrace|Ter)\b"
        r".*",
        re.I,
    )
    TIME_WINDOW_RE = re.compile(
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)\s*(?:-|–|—|to)\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)",
        re.I,
    )
    SINGLE_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM)|\d{1,2}\s*(?:AM|PM))\b", re.I)
    DATE_PATTERNS = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%A, %B %d, %Y",
        "%A, %b %d, %Y",
    )
    DATE_RE = re.compile(
        r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
        r"January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4})\b",
        re.I,
    )
    PO_RE = re.compile(r"\b(?:P\.?\s*O\.?|PO)\s*(?:#|number|no\.?)?\s*[:\-]?\s*([A-Z0-9\-]+)\b", re.I)
    PALLET_RE = re.compile(r"\b(\d+)\s*pallets?\b", re.I)
    WEIGHT_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", re.I)

    PICKUP_HINTS = ("pickup", "pick up", "origin", "shipper", "loading")
    DELIVERY_HINTS = ("delivery", "drop", "dropoff", "drop off", "consignee", "receiver", "destination")

    # ── Document-level rate-confirmation fields ─────────────────────────
    # (as opposed to the per-stop fields above, which are captured while
    # walking the document line-by-line for pickup/delivery structure)
    BOL_RE = re.compile(
        r"\b(?:Bill\s+of\s+Lading|BOL|B/L)\s*(?:#|number|no\.?)?\s*[:\-]\s*([A-Za-z0-9\-]+)\b",
        re.I,
    )
    RATE_RE = re.compile(
        r"\b(?:total\s+rate|line\s*haul\s+rate|freight\s+charge|carrier\s+rate|"
        r"flat\s+rate|agreed\s+rate|total\s+charge|rate\s+confirm(?:ed|ation)?\s+amount|"
        r"amount\s+due|rate)\s*[:\-]?\s*\$\s*([\d,]+\.\d{2})",
        re.I,
    )
    PAYMENT_TERMS_RE = re.compile(
        r"\b(Net\s*\d{1,3}|Due\s+on\s+Receipt|Quick\s*Pay|COD|Prepaid|Collect)\b", re.I
    )
    BROKER_RE = re.compile(
        r"\b(?:Broker|Bill\s*To|Customer(?:\s*Name)?|Shipper\s*Name)\s*[:\-]\s*([^\n]+)", re.I
    )
    LOAD_REF_RE = re.compile(
        r"\b(?:Load|Order|Confirmation)\s*#?\s*[:\-]\s*([A-Za-z0-9\-]+)\b", re.I
    )
    COMMODITY_RE = re.compile(r"\bCommodity\s*[:\-]\s*([^\n]+)", re.I)

    def __init__(self, env):
        self.env = env

    def parse_attachment(self, file_b64, mimetype="", filename="", extra_notes="", source_model="", source_id=0):
        text, method = self._extract_text(file_b64, mimetype, filename)
        merged_text = text
        if extra_notes:
            merged_text = (merged_text + "\n\n" + extra_notes).strip() if merged_text else extra_notes.strip()

        classification = self.classify_text(merged_text or text or "", filename=filename)
        parsed = self._parse_by_type(merged_text or text or "", classification["document_type"])
        result = {
            "document_type": classification["document_type"],
            "classification_confidence": classification["confidence"],
            "parser_method": method,
            "parser_used": parsed.get("parser_used", "none"),
            "confidence": parsed.get("confidence", 0.0),
            "stops": parsed.get("stops", []),
            "notes": parsed.get("notes", ""),
            "raw_text": (text or "")[:2000],
            "used_ai": False,
            "signals": classification.get("signals", []),
            "document_fields": self._extract_document_fields(merged_text or text or ""),
        }
        self.learn_to_ml(result, source_model=source_model, source_id=source_id, filename=filename)
        return result

    def _extract_document_fields(self, text):
        """Rate-confirmation fields that apply to the whole document rather
        than to a single stop: rate/freight amount, BOL #, PO #, payment
        terms, broker/customer name, load/reference #, equipment type,
        total weight, and commodity. Best-effort regex extraction, same
        philosophy as the rest of this service — empty/0 when not present,
        never fabricated, no paid API call involved."""
        if not text:
            return {}

        rate_amount = 0.0
        rate_match = self.RATE_RE.search(text)
        if rate_match:
            try:
                rate_amount = float(rate_match.group(1).replace(",", ""))
            except ValueError:
                rate_amount = 0.0

        bol_match = self.BOL_RE.search(text)
        payment_match = self.PAYMENT_TERMS_RE.search(text)
        broker_match = self.BROKER_RE.search(text)
        load_ref_match = self.LOAD_REF_RE.search(text)
        commodity_match = self.COMMODITY_RE.search(text)

        lower = text.lower()
        if "reefer" in lower or "refrigerated" in lower:
            equipment_type = "reefer"
        elif "flatbed" in lower:
            equipment_type = "flatbed"
        elif "dry van" in lower or re.search(r"\bvan\b", lower):
            equipment_type = "dry"
        else:
            equipment_type = ""

        weight_lbs = 0.0
        weight_matches = self.WEIGHT_RE.findall(text)
        if weight_matches:
            try:
                weight_lbs = max(float(w.replace(",", "")) for w in weight_matches)
            except ValueError:
                weight_lbs = 0.0

        return {
            "rate_amount": rate_amount,
            "bol_number": self._clean_line(bol_match.group(1)) if bol_match else "",
            "po_number": self._extract_po(text),
            "payment_terms": self._normalize_payment_term(payment_match.group(0)) if payment_match else "",
            "broker_name": self._clean_line(broker_match.group(1))[:120] if broker_match else "",
            "load_reference": self._clean_line(load_ref_match.group(1)) if load_ref_match else "",
            "equipment_type": equipment_type,
            "weight_lbs": weight_lbs,
            "commodity": self._clean_line(commodity_match.group(1))[:120] if commodity_match else "",
        }

    def _normalize_payment_term(self, raw):
        raw = self._clean_line(raw)
        if raw.upper() in ("COD", "QUICK PAY"):
            return raw.upper() if raw.upper() == "COD" else "Quick Pay"
        return " ".join(w.capitalize() for w in raw.split())

    def _extract_text(self, file_b64, mimetype="", filename=""):
        try:
            from odoo.addons.premafirm_ml.services.document_extractor import extract_from_b64
            return extract_from_b64(file_b64, mimetype, filename)
        except Exception as e:
            _logger.info("Dispatch document extractor unavailable or failed for %s: %s", filename, e)
            try:
                raw = base64.b64decode(file_b64) if file_b64 else b""
                if raw:
                    return raw.decode("utf-8", errors="ignore")[:6000], "plain_fallback"
            except Exception:
                _logger.exception("Fallback text extraction failed for %s", filename)
            return "", "none"

    def classify_text(self, text, filename=""):
        lower = (text or "").lower()
        scores = {
            "schedule_rate_confirmation": 0,
            "load_tender": 0,
            "route_sheet": 0,
            "invoice": 0,
            "unknown": 0,
        }
        signals = []

        def hit(doc_type, score, phrase):
            scores[doc_type] += score
            signals.append(f"{doc_type}:{phrase}")

        if "rate confirmation" in lower:
            hit("schedule_rate_confirmation", 4, "rate confirmation")
        if "weekly schedule" in lower or "schedule of service" in lower:
            hit("schedule_rate_confirmation", 4, "weekly schedule")
        if "total week" in lower:
            hit("schedule_rate_confirmation", 3, "total week")

        if "load tender" in lower:
            hit("load_tender", 4, "load tender")
        if "bill of lading" in lower or re.search(r"\bbol\b", lower):
            hit("load_tender", 2, "bol")
        if "shipper" in lower and "consignee" in lower:
            hit("load_tender", 3, "shipper+consignee")

        stop_markers = len(re.findall(r"\b(?:pickup|delivery|drop\s*off|consignee|shipper|stop)\b", lower))
        if stop_markers >= 4:
            hit("route_sheet", 3, f"stop-markers-{stop_markers}")
        if "route sheet" in lower or "stops" in lower:
            hit("route_sheet", 2, "route sheet/stops")

        if "invoice" in lower and "$" in lower:
            hit("invoice", 2, "invoice+$")

        doc_type = max(scores, key=scores.get)
        confidence = min(0.95, 0.35 + (scores[doc_type] * 0.12)) if scores[doc_type] else 0.1
        if doc_type == "unknown":
            confidence = 0.1

        if filename and filename.lower().endswith(".pdf") and doc_type == "unknown":
            confidence = 0.2

        return {
            "document_type": doc_type,
            "confidence": round(confidence, 2),
            "signals": signals[:10],
        }

    def _parse_by_type(self, text, document_type):
        if not text:
            return {"parser_used": "none", "confidence": 0.0, "stops": [], "notes": ""}
        if document_type == "schedule_rate_confirmation":
            return self._parse_schedule_rate_confirmation(text)
        return self._parse_stop_document(text)

    def _clean_line(self, line):
        return re.sub(r"\s+", " ", (line or "").replace("\xa0", " ")).strip()

    def _parse_schedule_rate_confirmation(self, text):
        stops = []
        lines = [self._clean_line(line) for line in text.splitlines()]
        week_match = re.search(r"Week:\s*([^\n]+)", text, re.I)
        pickup_match = re.search(r"Pickup Address\s+([^\n]+)", text, re.I)
        current_date = None
        current_amount = None
        for line in lines:
            if not line:
                continue
            if self.DATE_RE.search(line):
                current_date = self._parse_date_to_iso(self.DATE_RE.search(line).group(0))
            amount_match = re.search(r"\$([\d,]+\.\d{2})", line)
            if amount_match:
                current_amount = float(amount_match.group(1).replace(",", ""))
            lower = line.lower()
            if "pickup only" in lower or "delivery route" in lower:
                stop_type = "pickup" if "pickup only" in lower else "delivery"
                stops.append({
                    "type": stop_type,
                    "company_name": "",
                    "address": pickup_match.group(1).strip() if pickup_match else "",
                    "stop_date": current_date or "",
                    "time_window_start": "",
                    "time_window_end": "",
                    "po_number": "",
                    "stop_notes": line,
                    "liftgate": False,
                    "pallets": self._extract_pallets(text),
                    "weight_lbs": 0,
                    "amount": current_amount or 0.0,
                })
        if stops:
            return {
                "parser_used": "local_schedule_parser",
                "confidence": 0.88,
                "stops": stops,
                "notes": f"Schedule: {week_match.group(1).strip()}" if week_match else "",
            }
        return self._parse_stop_document(text)

    def _parse_stop_document(self, text):
        lines = [self._clean_line(line) for line in text.splitlines()]
        stops = []
        seen_addresses = set()
        current_type = ""
        for idx, line in enumerate(lines):
            if not line:
                continue
            lower = line.lower()
            if any(h in lower for h in self.PICKUP_HINTS):
                current_type = "pickup"
            elif any(h in lower for h in self.DELIVERY_HINTS):
                current_type = "delivery"

            address = self._extract_address(line)
            if not address:
                continue
            key = address.lower()
            if key in seen_addresses:
                continue
            seen_addresses.add(key)

            context_lines = [l for l in lines[max(0, idx - 2): idx + 3] if l]
            context = " | ".join(context_lines)
            stop_type = self._infer_stop_type(context, current_type, stop_index=len(stops))
            start, end = self._extract_time_window(context)
            stop_date = self._extract_date(context)
            company_name = self._extract_company(lines, idx, address)
            stop = {
                "type": stop_type,
                "company_name": company_name,
                "address": address,
                "stop_date": stop_date,
                "time_window_start": start,
                "time_window_end": end,
                "po_number": self._extract_po(context),
                "stop_notes": self._extract_notes(context),
                "liftgate": "liftgate" in context.lower(),
                "pallets": self._extract_pallets(context),
                "weight_lbs": self._extract_weight(context),
            }
            stops.append(stop)

        if len(stops) >= 2:
            normalized = []
            for i, stop in enumerate(stops):
                if i == 0 and stop["type"] != "pickup":
                    stop["type"] = "pickup"
                elif i > 0 and stop["type"] == "pickup" and not any(s["type"] == "delivery" for s in stops[i:]):
                    stop["type"] = "delivery"
                normalized.append(stop)
            richness = 0
            richness += sum(1 for stop in normalized if stop.get("time_window_start") or stop.get("time_window_end"))
            richness += sum(1 for stop in normalized if stop.get("company_name"))
            richness += sum(1 for stop in normalized if stop.get("po_number"))
            return {
                "parser_used": "local_stop_parser",
                "confidence": min(0.95, 0.62 + (len(normalized) * 0.05) + (richness * 0.02)),
                "stops": normalized,
                "notes": self._collect_general_notes(lines),
            }

        return {
            "parser_used": "local_stop_parser",
            "confidence": 0.2,
            "stops": stops,
            "notes": self._collect_general_notes(lines),
        }

    def _extract_address(self, line):
        match = self.STREET_RE.search(line or "")
        return self._clean_line(match.group(0)) if match else ""

    def _infer_stop_type(self, context, current_type, stop_index=0):
        lower = (context or "").lower()
        if any(h in lower for h in self.PICKUP_HINTS):
            return "pickup"
        if any(h in lower for h in self.DELIVERY_HINTS):
            return "delivery"
        if current_type:
            return current_type
        return "pickup" if stop_index == 0 else "delivery"

    def _extract_company(self, lines, idx, address):
        for offset in (1, 2):
            prev_idx = idx - offset
            if prev_idx < 0:
                continue
            candidate = self._clean_line(lines[prev_idx])
            if not candidate:
                continue
            lower = candidate.lower()
            if address.lower() in lower:
                continue
            if self.STREET_RE.search(candidate):
                continue
            if any(k in lower for k in ("pickup", "delivery", "shipper", "consignee", "date", "time", "window")):
                continue
            if len(candidate) <= 3:
                continue
            return candidate[:120]
        return ""

    def _extract_time_window(self, text):
        match = self.TIME_WINDOW_RE.search(text or "")
        if match:
            return self._normalize_time(match.group(1)), self._normalize_time(match.group(2))
        match = self.SINGLE_TIME_RE.search(text or "")
        if match:
            time_val = self._normalize_time(match.group(1))
            return time_val, ""
        return "", ""

    def _normalize_time(self, raw):
        raw = self._clean_line(raw).upper()
        if not raw:
            return ""
        for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
            try:
                return datetime.strptime(raw, fmt).strftime("%H:%M")
            except Exception:
                continue
        return raw

    def _extract_date(self, text):
        match = self.DATE_RE.search(text or "")
        return self._parse_date_to_iso(match.group(0)) if match else ""

    def _parse_date_to_iso(self, raw):
        raw = self._clean_line(raw).replace("Sept ", "Sep ")
        if "," in raw and raw.count(",") > 1:
            raw = raw.split(",", 1)[1].strip()
        for fmt in self.DATE_PATTERNS:
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
        return ""

    def _extract_po(self, text):
        match = self.PO_RE.search(text or "")
        return match.group(1).strip() if match else ""

    def _extract_pallets(self, text):
        match = self.PALLET_RE.search(text or "")
        return int(match.group(1)) if match else 0

    def _extract_weight(self, text):
        match = self.WEIGHT_RE.search(text or "")
        if not match:
            return 0.0
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return 0.0

    def _extract_notes(self, text):
        lines = []
        for piece in (text or "").split("|"):
            item = self._clean_line(piece)
            if not item:
                continue
            lower = item.lower()
            if any(word in lower for word in ("instruction", "note", "appt", "appointment", "dock", "call", "reference")):
                lines.append(item[:140])
        return " | ".join(lines[:3])

    def _collect_general_notes(self, lines):
        notes = []
        for line in lines:
            lower = line.lower()
            if any(word in lower for word in ("instruction", "notes", "driver must", "call before", "appointment", "dock", "liftgate")):
                notes.append(line[:160])
        return " | ".join(notes[:6])

    def learn_to_ml(self, parsed_result, source_model="", source_id=0, filename=""):
        if "premafirm.ml.knowledge" not in self.env:
            return None
        stops = parsed_result.get("stops") or []
        if not stops:
            return None
        try:
            kb = self.env["premafirm.ml.knowledge"].sudo()
            doc_type = parsed_result.get("document_type") or "unknown"
            context = [
                f"Document type: {doc_type}",
                f"Filename: {filename or ''}",
                f"Parser: {parsed_result.get('parser_used') or 'unknown'}",
                f"Stop count: {len(stops)}",
            ]
            for stop in stops[:8]:
                context.append(
                    f"{(stop.get('type') or '').upper()}: "
                    f"{' | '.join(v for v in [stop.get('company_name'), stop.get('address'), stop.get('stop_date')] if v)}"
                )
            output = {
                "document_type": doc_type,
                "confidence": parsed_result.get("confidence"),
                "stops": stops,
                "notes": parsed_result.get("notes", ""),
            }
            key = "\n".join(context)
            exists = kb.search([
                ("knowledge_type", "=", "load_tender"),
                ("origin", "=", "ingested"),
                ("input_context", "=", key),
            ], limit=1)
            if exists:
                return exists
            return kb.create({
                "knowledge_type": "load_tender",
                "input_context": key,
                "good_output": json.dumps(output, indent=2),
                "origin": "ingested",
                "weight": 0.7 if parsed_result.get("confidence", 0) >= 0.7 else 0.4,
            })
        except Exception:
            _logger.exception("Failed to persist dispatch parser result to ML")
            return None
