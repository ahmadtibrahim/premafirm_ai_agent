# -*- coding: utf-8 -*-
"""Shared customer-facing freight display formatting for AI Generate.

ONE formatting standard for the quotation AND invoice note blocks:

    Freight / Delivery Service
    Route: Cobourg, ON → Mississauga, ON
    Date: September 11, 2026
    Pickup: 11:00 AM–12:00 PM
    Delivery: 2:30 PM–3:30 PM
    Load: 12 pallets / 12,000 lb
    Commodity: Frozen Bakery
    Temperature: -20°C
    Note: <only a short, meaningful operational instruction>

Pure functions — no Odoo env dependency — so every flow can share them.

Display rules (keep the customer-facing note CLEAN):
* Reference numbers (PO/BOL/load ref) are deliberately NOT part of the
  visible block: they live in their dedicated fields, the AI summary and the
  internal extraction snapshot.
* Operational clutter (booking URLs, individual contacts, phone numbers, DC
  emails, delivery IDs, long instructions) never reaches the visible note —
  build_operational_details() collects it for the AI summary / internal
  metadata instead.
* Nothing is invented: a line is emitted only when its data was extracted.
"""
import re

SERVICE_HEADER = "Freight / Delivery Service"

# Province / state codes used to classify a lane as Canada vs US.
_CANADA_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}
_US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}

# Special instructions longer than this — or containing contact/booking
# machinery — never reach the customer-facing note.
_NOTE_MAX_LEN = 180
_NOTE_CLUTTER_MARKERS = ("http", "www.", "@", "tel:", "phone", "ext.", "mailto:")


def _clean(value):
    return str(value or "").strip()


def _norm_word(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# ── date / window helpers (shared with the invoice flow) ─────────────────

def fmt_service_date(date_value):
    """YYYY-MM-DD (or a close variant) → 'September 11, 2026'; '' when the
    value cannot be parsed safely. Never invents a date."""
    if not date_value:
        return ""
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(date_value))
    if not m:
        return ""
    try:
        from datetime import date as _date
        parsed = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    return parsed.strftime("%B %d, %Y")


def normalize_appointment_window(text):
    """Window text → '11:00 AM–12:00 PM' (en dash); '' when blank.

    Handles '11:00 AM - 12:00 PM' and '11:00 AM to 12:00 PM', and drops a
    leading zero on a 12-hour hour ('02:30 PM' → '2:30 PM')."""
    text = _clean(text)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # 'to' between two clock times → en dash (case-insensitive)
    text = re.sub(
        r"(\b\d{1,2}:\d{2}(?:\s*[APap]\.?[Mm]\.?)?)\s+[tT][oO]\s+"
        r"(\d{1,2}:\d{2}\s*(?:[APap]\.?[Mm]\.?)?)",
        r"\1–\2", text)
    text = re.sub(r"\s*[-–—]\s*", "–", text)
    # '02:30 PM' → '2:30 PM' (12-hour style only)
    text = re.sub(r"\b0(\d:\d{2}\s*[APap]\.?[Mm]\.?)", r"\1", text)
    return text.strip(" –")


# ── shipment-context classifiers (product selection) ─────────────────────

def shipment_region(result, partner_country_code=""):
    """'ca' | 'us' | '' — from pickup/delivery regions, then the partner's
    country as a fallback. Empty when nothing points either way."""
    regions = [str((result.get("pickup_region") or "")).upper(),
               str((result.get("delivery_region") or "")).upper()]
    if any(r in _CANADA_REGIONS for r in regions):
        return "ca"
    if any(r in _US_REGIONS for r in regions):
        return "us"
    if partner_country_code:
        code = partner_country_code.upper()
        if code == "CA":
            return "ca"
        if code == "US":
            return "us"
    return ""


def shipment_temp_kind(result):
    """'reefer' | 'dry' | '' — from temp_control, temperature and equipment.
    Any actual temperature spec (e.g. -20°C, 4°C) or frozen/chilled wording
    means a temperature-controlled (reefer) load; 'dry'/'ambient' wording
    means dry. Never inferred from the commodity name alone."""
    control = str((result.get("temp_control") or "")).lower()
    if control in ("frozen", "chilled"):
        return "reefer"
    if control in ("dry", "ambient"):
        return "dry"
    temperature = str((result.get("temperature") or "")).lower()
    if temperature:
        if any(k in temperature for k in
               ("frozen", "chill", "reefer", "ice", "dry", "ambient")):
            return "reefer" if any(k in temperature for k in
                                   ("frozen", "chill", "reefer", "ice")) else "dry"
        # Any numeric temperature spec is a controlled-temp load.
        if re.search(r"\d", temperature):
            return "reefer"
    equipment = str((result.get("equipment") or "")).lower()
    if "reefer" in equipment:
        return "reefer"
    if "dry" in equipment and "van" in equipment:
        return "dry"
    return ""


def normalized_service_type(result):
    """service_type normalized to the known catalog set, else ''."""
    st = str((result.get("service_type") or "")).strip().lower()
    return st if st in ("ltl", "ftl", "local", "dedicated", "other") else ""


# ── the visible service note ─────────────────────────────────────────────

_ANNOTATION_WORDS = re.compile(
    r"\b(as per|per\s+(rc|po|rate|doc|quote|confirmation)|"
    r"mixed|incl\.?uding|see|ref|note|per)\b", re.IGNORECASE)


def _plain_commodity(value):
    """Commodity text → plain name. LLMs sometimes append an explanatory
    parenthetical ('Frozen Bakery (mixed dry & frozen product lines as per
    RC)'); a parenthetical that reads as commentary is dropped so the visible
    block shows the commodity itself. A genuine parenthetical (e.g. a
    packaging note) is kept."""
    text = _clean(value)
    if not text:
        return ""
    m = re.search(r"\s*\(([^()]*)\)\s*$", text)
    if m and m.start() >= 2 and _ANNOTATION_WORDS.search(m.group(1)):
        head = text[:m.start()].strip().rstrip(" ,;:-")
        return head or text
    return text


def _short_clean_instruction(instructions):
    """Return the instruction text ONLY when it is short and free of
    contact/booking machinery (URLs, emails, phones…) — those stay internal.
    A leading 'Note:' label already on the text is stripped (build_service_note
    adds its own 'Note: ' prefix)."""
    text = _clean(instructions)
    if not text or len(text) > _NOTE_MAX_LEN:
        return ""
    low = text.lower()
    if any(marker in low for marker in _NOTE_CLUTTER_MARKERS):
        return ""
    if low.startswith("note:"):
        text = text[5:].strip(" -–—\t").strip()
        if not text:
            return ""
    if "\n" in text:
        # keep only the first sentence of a short multi-line instruction
        first = text.split("\n")[0].strip()
        if first and len(first) <= _NOTE_MAX_LEN:
            return first
        return ""
    return text


def build_service_note(result):
    """Deterministic visible note block — see module docstring for the exact
    standard. Returns '' when there is nothing factual to show (callers then
    skip the note line entirely)."""
    if not result:
        return ""
    lines = [SERVICE_HEADER]

    # Route: cities with region when available; falls back to the concrete
    # stop/location name — never invents a lane.
    def _place(city, region, name=""):
        city = _clean(city)
        if city:
            geo = _clean(region)
            return f"{city}, {geo}" if geo else city
        return _clean(name)

    origin = _place(result.get("pickup_city"), result.get("pickup_region"),
                    result.get("pickup_location_name"))
    dest = _place(result.get("delivery_city"), result.get("delivery_region"),
                  result.get("delivery_location_name"))
    if origin and dest:
        lines.append(f"Route: {origin} → {dest}")

    # Date: the service date — one line when pickup/delivery share a day.
    pickup_date = fmt_service_date(result.get("pickup_date"))
    delivery_date = fmt_service_date(result.get("delivery_date"))
    if pickup_date and pickup_date == delivery_date:
        lines.append(f"Date: {pickup_date}")
    elif pickup_date and delivery_date:
        lines.append(f"Date: {pickup_date} → {delivery_date}")
    elif pickup_date:
        lines.append(f"Date: {pickup_date}")
    elif delivery_date:
        lines.append(f"Date: {delivery_date}")

    # Appointment windows (kept as written, dash-normalized).
    pickup_appt = normalize_appointment_window(result.get("pickup_appointment"))
    delivery_appt = normalize_appointment_window(result.get("delivery_appointment"))
    if pickup_appt:
        lines.append(f"Pickup: {pickup_appt}")
    if delivery_appt:
        lines.append(f"Delivery: {delivery_appt}")

    # Load: pallets / weight on one line.
    load_bits = []
    try:
        pallets = int(float(result.get("pallets") or 0))
    except (TypeError, ValueError):
        pallets = 0
    if pallets > 0:
        load_bits.append(f"{pallets} pallets")
    try:
        weight = float(result.get("weight") or 0.0)
    except (TypeError, ValueError):
        weight = 0.0
    if weight > 0:
        label = f"{weight:,.0f}" if weight == int(weight) else f"{weight:,.1f}"
        unit = _clean(result.get("weight_unit")) or "lb"
        if unit.lower() in ("lbs", "pounds", "lb"):
            unit = "lb"  # canonical customer-facing form
        load_bits.append(f"{label} {unit}")
    if load_bits:
        lines.append("Load: " + " / ".join(load_bits))

    commodity = _plain_commodity(result.get("commodity"))
    if commodity:
        lines.append(f"Commodity: {commodity}")

    temperature = _clean(result.get("temperature"))
    if temperature:
        lines.append(f"Temperature: {temperature}")
    else:
        control = _clean(result.get("temp_control"))
        if control:
            lines.append(f"Temperature: {control.capitalize()}")

    # Short note — ONLY when operationally meaningful.
    note = _short_clean_instruction(result.get("special_instructions"))
    if not note:
        if pickup_appt and delivery_appt:
            note = "Pickup and delivery appointments required."
        elif pickup_appt:
            note = "Pickup appointment required."
        elif delivery_appt:
            note = "Delivery appointment required."
    if note:
        lines.append(f"Note: {note}")

    if len(lines) == 1:
        return ""  # header alone — nothing factual to display
    return "\n".join(lines)


# ── internal operational details (AI summary / extraction snapshot) ──────

def build_operational_details(result):
    """One line per internally-useful detail that is deliberately NOT shown
    on the customer-facing note: shipper/customer names, requested
    equipment, non-charge accessorials and the full pickup/delivery
    instructions (booking URLs, contacts, phones, DC emails, delivery
    IDs…). Returns '' when nothing is available."""
    if not result:
        return ""
    bits = []
    for label, key in (
        ("Shipper", "shipper"),
        ("Customer", "customer"),
        ("Equipment requested", "equipment"),
        ("Accessorials", "accessorials"),
    ):
        value = _clean(result.get(key))
        if value:
            bits.append(f"{label}: {value}")
    service_name = _clean(result.get("service_name"))
    if service_name and not _clean(result.get("equipment")):
        bits.append(f"Service: {service_name}")
    instructions = _clean(result.get("special_instructions"))
    if instructions:
        bits.append(f"Instructions: {instructions}")
    return "\n".join(bits)
