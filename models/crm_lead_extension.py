import logging
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    # ── Business reference fields ──────────────────────────────
    company_currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
        store=True,
    )
    final_rate = fields.Monetary(currency_field="company_currency_id", default=0.0)
    product_id = fields.Many2one("product.product", string="Freight Product")
    po_number = fields.Char("Customer PO #")
    bol_number = fields.Char("BOL #")
    pod_reference = fields.Char("POD Reference")
    payment_terms = fields.Many2one("account.payment.term", string="Payment Terms")

    # Backward-compatible aliases
    premafirm_po = fields.Char(related="po_number", store=True, readonly=False)
    premafirm_bol = fields.Char(related="bol_number", store=True, readonly=False)
    premafirm_pod = fields.Char(related="pod_reference", store=True, readonly=False)

    # reply_received is now computed/stored from last_meaningful_reply_at
    # (see crm_reply_status.py, PHASE 8).
    next_followup_date = fields.Date("Next Follow-up")
    ai_lead_score = fields.Float("AI Lead Score", digits=(5, 1), default=0.0)

    def action_fetch_emails(self):
        self.env['fetchmail.server']._fetch_mails()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Emails Refreshed",
                "message": "Incoming mail servers have been checked for new emails.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def message_post(self, **kwargs):
        # No email_from injection — Odoo's own default sender is used
        # (business decision 2026-08-20: tag-based From override removed).
        # PHASE 29 — preserve the caller's threading intent for the
        # reply-status hook.  message_post re-computes parent_id under
        # flat threading BEFORE _message_post_after_hook runs, so a fresh
        # composer email and a reply both come back with the same computed
        # parent (the thread's first message) and the hook cannot tell
        # outreach from an answer.  The ORIGINAL values (parent_id /
        # references / in_reply_to as passed by the composer or caller)
        # say whether we continue an existing email thread.
        if ('parent_id' in kwargs or 'references' in kwargs
                or 'in_reply_to' in kwargs):
            self = self.with_context(premafirm_post_intent=bool(
                kwargs.get('parent_id') or kwargs.get('references')
                or kwargs.get('in_reply_to')))
        return super().message_post(**kwargs)

    # ── Tag inheritance from the linked contact/company ───────────
    # Mirrors partner_id.category_id (Contact Tags, incl. the Province/City
    # tags stamped on the parent company) onto this lead's CRM Tags.

    def _sync_tags_from_partner(self):
        Tag = self.env['crm.tag']
        tag_cache = {}
        for lead in self:
            partner = lead.partner_id
            if not partner or not partner.category_id:
                continue
            tag_ids = []
            for cat in partner.category_id:
                name = (cat.name or '').strip()
                if not name:
                    continue
                if name not in tag_cache:
                    tag = Tag.search([('name', '=', name)], limit=1)
                    if not tag:
                        tag = Tag.create({'name': name})
                    tag_cache[name] = tag.id
                tag_ids.append(tag_cache[name])
            missing = set(tag_ids) - set(lead.tag_ids.ids)
            if missing:
                lead.write({'tag_ids': [(4, tid) for tid in missing]})

    @api.model_create_multi
    def create(self, vals_list):
        # PHASE 11 — the routed new-inquiry path passes the sender's
        # partner id in this marker key (popped before super so the
        # unknown field never reaches the DB).
        attach = [vals.pop('premafirm_attach_contact', False)
                  for vals in vals_list]
        leads = super().create(vals_list)
        for lead, author in zip(leads, attach):
            # company → contacts: the opportunity's partner is the COMPANY;
            # a contact-child sender is tracked as a contact row instead.
            if lead.partner_id and lead.partner_id.parent_id:
                lead.write({'partner_id': lead.partner_id.parent_id.id})
            if author:
                self.env['crm.lead.contact']._attach_sender(lead.id, author)
        leads.filtered('partner_id')._sync_tags_from_partner()
        return leads

    def write(self, vals):
        result = super().write(vals)
        if 'partner_id' in vals:
            self.filtered('partner_id')._sync_tags_from_partner()
        return result


# ══════════════════════════════════════════════════════════════════════════
# Website callback / quote-request ingestion (automation 63 → action 1515)
#
# The /request-a-callback website form posts to the standard /website/form
# endpoint and creates a crm.lead (hidden name "Callback Request", team 1).
# Automation 63 then runs this pipeline: normalize the customer's
# identity, build the company → contact partner hierarchy with duplicate
# prevention, and land the lead in the QUOTE REQUESTED stage — resolved by
# name, never by a hardcoded database id.
# ══════════════════════════════════════════════════════════════════════════

_CALLBACK_QUOTE_STAGE = "QUOTE REQUESTED"

# Consumer mail domains identify a private person, not a business. Never
# used to match an existing company (§6 priority 1).
_CALLBACK_DISPOSABLE_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "live.com",
    "icloud.com", "aol.com", "me.com", "msn.com", "ymail.com", "gmx.com",
    "googlemail.com", "hotmail.ca", "outlook.ca", "yahoo.ca",
    "protonmail.com", "zoho.com", "mail.com",
}

# Canadian provinces/territories recognised when a company-name field
# carries a full "Company, Street, City, PROV, Canada" address (browser
# autofill of the free-text company field).
_CALLBACK_CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC",
    "SK", "YT",
}

_CALLBACK_PROVINCE_NAMES = {
    "ontario": "ON", "quebec": "QC", "alberta": "AB", "britishcolumbia": "BC",
    "manitoba": "MB", "newbrunswick": "NB", "newfoundlandandlabrador": "NL",
    "novascotia": "NS", "northwestterritories": "NT", "nunavut": "NU",
    "princeedwardisland": "PE", "saskatchewan": "SK", "yukon": "YT",
}

_CALLBACK_STREET_HINTS = re.compile(
    r"\b(?:ave|avenue|blvd|boulevard|st|street|rd|road|dr|drive|crt|court|"
    r"cres|crescent|hwy|highway|way|lane|ln|trail|place|gate)\b", re.I)


def _callback_norm_email(email):
    """Canonical email key — lowercased and trimmed (§8)."""
    return (email or "").strip().lower()


def _callback_norm_name(name):
    """Normalize a company/person name for exact comparison: lowercase,
    collapse whitespace, drop harmless punctuation (. ,). Never fuzzy —
    similar names (Hung Shing vs Hung Sing) are different companies."""
    s = (name or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(".", "").replace(",", "")
    return s.lower().strip()


def _callback_phone_key(phone):
    """Digits-only phone identity: strip every non-numeric character, drop
    a leading country code 1 when 11 digits remain (§9). Used for both
    stored-value comparison and verification — display and matching never
    disagree."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _callback_phone_e164(phone, country_code=False):
    """Best-effort E164 display form (+1… for Canadian numbers). Falls back
    to the raw value when the number cannot be normalized."""
    digits = _callback_phone_key(phone)
    if len(digits) == 10 and country_code == "CA":
        return "+1" + digits
    return phone or ""


def _callback_postal_key(code):
    return re.sub(r"\s+", "", code or "").upper()


def _callback_company_address_split(raw):
    """'Hung Shing Meat Trading Ltd, Commander Boulevard, Scarborough, ON,
    Canada' → ('Hung Shing Meat Trading Ltd', {'street': '', 'street2':
    'Commander Boulevard', 'city': 'Scarborough', 'state': 'ON', 'zip': '',
    'country': 'CA'}).

    The form's company-name field is free text and browser autofill
    routinely fills it with a full formatted address. Detect that case and
    return the company name with the address split into structured parts
    so the partner name is never polluted with street text (§2/§5).
    Returns (raw, None) when the input does not look like company + address.
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if len(parts) < 2:
        return raw, None
    tail = parts[1:]
    addr = {"street": "", "street2": "", "city": "", "state": "",
            "zip": "", "country": ""}

    # Trailing country ("Canada" / "CA")
    if re.match(r"^ca(nada)?$", tail[-1], re.I):
        addr["country"] = "CA"
        tail = tail[:-1]
    if not tail:
        return raw, None

    # Province/postal in the last segment ("ON" / "Ontario" /
    # "ON M1S 3E7" / "Toronto, ON M5V 1A1" variants). The postal code is
    # extracted first — it also exposes the province code glued to it.
    state = False
    last = tail[-1]
    postal = re.search(r"[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d", last)
    if postal:
        addr["zip"] = _callback_postal_key(postal.group(0))
        pcode = re.sub(r"[.\s]", "", last[:postal.start()]).upper()
        if pcode in _CALLBACK_CA_PROVINCES:
            state = pcode
        tail = tail[:-1]
    else:
        seg_code = re.sub(r"[.\s]", "", last).lower()
        if seg_code.upper() in _CALLBACK_CA_PROVINCES:
            state = seg_code.upper()
        elif seg_code in _CALLBACK_PROVINCE_NAMES:
            state = _CALLBACK_PROVINCE_NAMES[seg_code]
        if state:
            tail = tail[:-1]
    if state:
        addr["state"] = state

    # Remaining segments: street-ish parts (street keyword / number) first,
    # then the locality. A numberless street name goes to street2 so a
    # separately-typed "23 Commander Boulevard" street is never lost.
    street, street2, city = [], [], []
    for seg in tail:
        looks_street = bool(_CALLBACK_STREET_HINTS.search(seg)) or \
            bool(re.search(r"\d", seg))
        if looks_street and not city:
            (street if re.search(r"\d", seg) else street2).append(seg)
        else:
            city.append(seg)
    addr["street"] = ", ".join(street)
    addr["street2"] = ", ".join(street2)
    addr["city"] = ", ".join(city)

    if not (addr["street"] or addr["street2"] or addr["city"]
            or addr["state"]):
        return raw, None
    return parts[0].strip(), addr


class CrmLeadWebsiteCallback(models.Model):
    """Website callback/quote-request ingestion (extends crm.lead).

    Split from the CrmLead extension above so the ingestion pipeline stays
    reviewable; automation 63's server action (ir.actions.server 1515) is
    a one-line call into :meth:`prema_process_website_callback`.
    """
    _inherit = "crm.lead"

    # ── §6 duplicate-company prevention ──────────────────────────────
    def _callback_find_company(self, company_name, email, addr):
        """Priority: 1 exact company by normalized business email domain
        (never consumer domains); 2 exact normalized name + postal/address;
        3 exact normalized name unambiguous; 4 create new (caller)."""
        Partner = self.env["res.partner"]
        name = _callback_norm_name(company_name)
        if not name:
            return Partner.browse()

        # P1 — business email domain → owning company (via any partner that
        # holds an address on that domain: usually the contact's own email).
        domain = ""
        if email and "@" in email:
            domain = email.rsplit("@", 1)[1].lower()
        if domain and domain not in _CALLBACK_DISPOSABLE_DOMAINS:
            holder = Partner.search([("email", "=ilike", "%@" + domain)],
                                    limit=1)
            if holder:
                company = holder if holder.is_company else holder.parent_id
                if company and company.is_company:
                    return company

        # Candidate pool: active companies starting with the name's first
        # word (bounded), then exact normalized comparison in Python.
        first_word = name.split(" ", 1)[0] if " " in name else name
        candidates = Partner.search([
            ("is_company", "=", True),
            ("name", "ilike", first_word + "%"),
        ])
        exact = candidates.filtered(
            lambda c: _callback_norm_name(c.name) == name)
        if not exact:
            return Partner.browse()
        if len(exact) == 1:
            return exact                       # P3 — unambiguous

        # P2 — narrow the same-name set by postal / street / city.
        narrowed = exact
        if addr and addr.get("zip"):
            zkey = _callback_postal_key(addr["zip"])
            narrowed = narrowed.filtered(
                lambda c: c.zip and _callback_postal_key(c.zip) == zkey)
        elif addr and addr.get("street"):
            skey = _callback_norm_name(addr["street"])
            narrowed = narrowed.filtered(
                lambda c: c.street
                and _callback_norm_name(c.street) == skey)
        elif addr and addr.get("city"):
            ckey = _callback_norm_name(addr["city"])
            narrowed = narrowed.filtered(
                lambda c: c.city and _callback_norm_name(c.city) == ckey)
        if len(narrowed) == 1:
            return narrowed
        return Partner.browse()                # ambiguous → create new

    def _callback_company_vals(self, company_name, addr, phone_e164):
        """Fresh company partner values — name carries NO address text;
        street/city/state/zip/country go to their own fields (§2)."""
        vals = {"name": company_name, "is_company": True}
        if addr:
            for key, value in addr.items():
                if not value:
                    continue
                if key == "state":
                    state = self.env["res.country.state"].search([
                        ("code", "=", value), ("country_id", "=", 38)],
                        limit=1)
                    if state:
                        vals["state_id"] = state.id
                elif key == "country":
                    country = self.env["res.country"].search(
                        [("code", "=", value)], limit=1)
                    if country:
                        vals["country_id"] = country.id
                else:
                    vals[key] = value
        if phone_e164:
            vals["phone"] = phone_e164
        return vals

    def _callback_backfill_company(self, company, addr, phone_e164):
        """Fill only the address fields the existing company still lacks."""
        upd = {}
        if addr:
            for key, value in addr.items():
                if not value:
                    continue
                if key == "state":
                    if not company.state_id:
                        state = self.env["res.country.state"].search([
                            ("code", "=", value),
                            ("country_id", "=", 38)], limit=1)
                        if state:
                            upd["state_id"] = state.id
                elif key == "country":
                    if not company.country_id:
                        country = self.env["res.country"].search(
                            [("code", "=", value)], limit=1)
                        if country:
                            upd["country_id"] = country.id
                elif not company[key]:
                    upd[key] = value
        if phone_e164 and not company.phone:
            upd["phone"] = phone_e164
        if upd:
            company.write(upd)

    # ── §7 duplicate-person prevention ───────────────────────────────
    def _callback_find_contact(self, contact_name, email, phone_key,
                               company):
        """Priority: 1 normalized exact email; 2 normalized phone;
        3 exact normalized name under the matched company. A person found
        under ANOTHER company is never moved here and never reused as this
        company's contact; an orphan (parent_id False) is linked to the
        company only when safe."""
        Partner = self.env["res.partner"]
        norm = _callback_norm_name(contact_name)

        # P1 — email
        if email:
            hit = Partner.search([("email", "=ilike", email)], limit=1)
            if hit and not hit.is_company:
                if not hit.parent_id or (company
                                         and hit.parent_id.id == company.id):
                    return hit

        # P2 — phone (suffix-anchored search, full-key verification)
        if phone_key and len(phone_key) >= 7:
            cands = Partner.search([("phone", "ilike", phone_key[-7:])])
            hits = cands.filtered(
                lambda p: _callback_phone_key(p.phone) == phone_key
                and not p.is_company)
            if len(hits) == 1:
                hit = hits
                if not hit.parent_id or (company
                                         and hit.parent_id.id == company.id):
                    return hit

        # P3 — exact normalized name under the matched company
        if company and norm:
            under = Partner.search([("parent_id", "=", company.id)])
            exact = under.filtered(
                lambda p: not p.is_company
                and _callback_norm_name(p.name) == norm)
            if len(exact) == 1:
                return exact
        return Partner.browse()

    # ── the pipeline (server action 1515 calls this) ─────────────────
    def prema_process_website_callback(self):
        """Normalize + link a website callback/quote-request lead.

        Runs inside automation 63 ('Callback Request - tag, notify, note')
        after a /request-a-callback submission creates the lead. Every
        record is resolved by name or canonical identity — never by a
        hardcoded database id — and the lead's freight requirements
        (description), chatter, tags, team notification and attention flag
        are all preserved.
        """
        Partner = self.env["res.partner"]
        Stage = self.env["crm.stage"]
        Tag = self.env["crm.tag"]

        quote_stage = Stage.search(
            [("name", "=", _CALLBACK_QUOTE_STAGE)], limit=1)
        if not quote_stage:
            quote_stage = Stage.search(
                [("name", "ilike", "quote requested")], limit=1)
        callback_tag = Tag.search(
            [("name", "=", "Callback Request")], limit=1)

        for lead in self:
            if (not lead.phone or not lead.contact_name
                    or not lead.partner_name or not lead.email_from):
                raise UserError(
                    "Callback request is missing a required field "
                    "(name, phone, email or company name).")

            # ── normalize the raw form payload (§8/§9/§2) ──────────────
            email = _callback_norm_email(lead.email_from)
            phone_e164 = _callback_phone_e164(
                lead.phone, lead.country_id.code if lead.country_id else "CA")
            phone_key = _callback_phone_key(lead.phone)
            company_name, addr = _callback_company_address_split(
                lead.partner_name)

            # ── lead basics: type, stage, attention ────────────────────
            update_vals = {"type": "opportunity"}
            if quote_stage and lead.stage_id.id != quote_stage.id:
                update_vals["stage_id"] = quote_stage.id   # name-resolved
            if not lead.x_needs_attention:
                update_vals["x_needs_attention"] = True

            # ── company partner (dedupe §6) ────────────────────────────
            company = self._callback_find_company(
                company_name or "", email, addr)
            if not company and company_name:
                company = Partner.create(self._callback_company_vals(
                    company_name, addr, phone_e164))
            if company:
                self._callback_backfill_company(company, addr, phone_e164)

            # ── contact person (dedupe §7) ─────────────────────────────
            contact = self._callback_find_contact(
                lead.contact_name, email, phone_key, company)
            if not contact:
                contact = Partner.create({
                    "name": lead.contact_name.strip(),
                    "is_company": False,
                    "parent_id": company.id if company else False,
                    "email": email or False,
                    "phone": phone_e164 or lead.phone or False,
                })
            else:
                c_upd = {}
                if email and not contact.email:
                    c_upd["email"] = email
                if phone_e164 and not contact.phone:
                    c_upd["phone"] = phone_e164
                if company and not contact.parent_id:
                    c_upd["parent_id"] = company.id  # link orphan: safe
                if c_upd:
                    contact.write(c_upd)

            # ── lead linkage (§4): partner = COMPANY, person tracked ───
            # apart on the dedicated logistics-contact field.
            update_vals.update({
                "partner_name": company_name or lead.partner_name,
                "email_from": email or lead.email_from,
                "phone": phone_e164 or lead.phone,
                "contact_name": lead.contact_name.strip(),
                "logistics_contact_id": contact.id if contact else False,
            })
            if company:
                update_vals["partner_id"] = company.id
            # structured address parts back onto the lead too (form
            # semantics: street/city/zip are the company-address fields)
            if addr:
                for fname in ("street", "street2", "city", "zip"):
                    if addr.get(fname):
                        update_vals[fname] = addr[fname]
                if addr.get("state"):
                    state = self.env["res.country.state"].search([
                        ("code", "=", addr["state"]),
                        ("country_id", "=", 38)], limit=1)
                    if state:
                        update_vals["state_id"] = state.id
                if addr.get("country"):
                    country = self.env["res.country"].search(
                        [("code", "=", addr["country"])], limit=1)
                    if country:
                        update_vals["country_id"] = country.id

            # ── informative title (§11), still matched by automation 63 ─
            if not lead.name or lead.name == "Callback Request":
                update_vals["name"] = (
                    "%s — Quote Request" % company_name
                    if company_name else "Quote Request")

            lead.write(update_vals)

            # ── preserved behaviour: freight notes, tag, notify ─────────
            if lead.description:
                lead.message_post(
                    body="<p><strong>Customer's stated requirements:</strong></p>"
                         + lead.description,
                    subtype_xmlid="mail.mt_note",
                )
            if callback_tag and callback_tag.id not in lead.tag_ids.ids:
                lead.write({"tag_ids": [(4, callback_tag.id)]})
            team = lead.team_id
            if team:
                partner_ids = team.crm_team_member_ids.user_id.partner_id.ids
                if partner_ids:
                    lead.message_subscribe(partner_ids=partner_ids)
            lead.message_post(
                body="New callback request submitted via the website - "
                     "please call the customer back.")
