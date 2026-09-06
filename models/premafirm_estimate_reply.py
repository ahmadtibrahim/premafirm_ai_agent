"""
Preliminary Estimate Reply drafts (MP1 E-A2, master instruction §3.3).

One fully-discoverable, editable DRAFT per staff trigger: the model stores the
generated subject/body, the price the staff member approved for the draft
(ALWAYS passed in by the caller — the engine never invents a price), where
that price came from (``price_reference``), and the shipment-fact snapshot
with provenance that the draft was built from (``facts_snapshot``).

Hard safety guarantees
----------------------
* This model has NO send method and this module never creates mail.mail
  records.  The reply is a NON-BINDING draft: nothing is auto-sent, nothing
  is priced autonomously, nothing is confirmed.
* Price text is injected into the body programmatically from the
  ``price_amount``/``currency``/``price_reference`` parameters AFTER the LLM
  generates the prose, and the LLM prompt forbids the model from writing any
  price — the price can therefore never be invented by the AI.
* A concise non-binding disclaimer is appended programmatically with the
  price line.
* Acceptance/revision remains a FLAG decision by staff on the opportunity
  (x_needs_attention et al.) — creating or editing this draft never moves a
  pipeline state and never writes to the customer.
"""

import html
import logging
import re
from datetime import datetime

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Human labels for the shared fact vocabulary (fall back to a title-cased
# field name when a label is missing here).
FIELD_LABELS = {
    "reference": "Reference",
    "commodity": "Commodity",
    "equipment": "Equipment",
    "temperature_mode": "Temperature requirement",
    "temperature_setpoint": "Temperature setpoint",
    "package_type": "Packaging",
    "pallets": "Pallets",
    "cases": "Cases",
    "pieces": "Pieces",
    "weight_lbs": "Weight",
    "dimensions": "Dimensions",
    "pickup_date": "Pickup date",
    "pickup_earliest": "Pickup window from",
    "pickup_latest": "Pickup window until",
    "delivery_date": "Delivery date",
    "delivery_deadline": "Delivery deadline",
    "service_minutes": "Service time at delivery (minutes)",
    "origin_address": "Pickup address",
    "origin_city": "Pickup city",
    "origin_postal_code": "Pickup postal code",
    "destination_address": "Delivery address",
    "destination_city": "Delivery city",
    "destination_postal_code": "Delivery postal code",
    "stops": "Additional stops",
    "accessorials": "Accessorials",
    "contacts": "Contacts",
    "instructions": "Instructions",
    "document_number": "Document number",
}

NON_BINDING_NOTE = (
    "This is a preliminary, NON-BINDING draft: no rate, price or capacity is "
    "guaranteed until a formal Customer Rate Confirmation is issued and "
    "accepted by both parties. Nothing here has been sent to the customer."
)


def _iso_naive_utc(at):
    """Serialize any timestamp as naive-UTC ISO (None passes through)."""
    if at is None:
        return None
    if isinstance(at, datetime):
        if at.tzinfo is not None:
            at = at.astimezone(pytz.utc).replace(tzinfo=None)
        return at.isoformat(sep=" ", timespec="seconds")
    return str(at)


def _fact_line(fact):
    """One human-readable, visibly-sourced fact line."""
    field = (fact.get("field") or "").lower()
    label = FIELD_LABELS.get(field, field.replace("_", " ").title())
    value = str(fact.get("value") or "")
    source = str(fact.get("source") or "")
    confidence = str(fact.get("confidence") or "")
    when = str(fact.get("at") or "")[:10]
    provenance = ", ".join(p for p in (source, when) if p)
    return "%s: %s — %s (confidence: %s)" % (
        label, value, provenance or "customer documents", confidence)


class PremafirmLeadEstimateReply(models.Model):
    """An editable, non-binding preliminary-estimate reply draft."""

    _name = "premafirm.lead.estimate.reply"
    _description = "Preliminary Estimate Reply Draft (non-binding, never auto-sent)"
    _order = "create_date desc, id desc"
    _rec_name = "subject"

    crm_lead_id = fields.Many2one(
        "crm.lead", string="Opportunity", ondelete="cascade", index=True,
        required=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer", readonly=True,
        help="Snapshot of the customer at draft time.")
    subject = fields.Char(string="Subject")
    body_html = fields.Html(
        string="Draft reply", sanitize=True, strip_style=True,
        help="Editable draft. Not sent to anyone by this module.")
    price_amount = fields.Monetary(
        string="Estimate price", currency_field="currency_id",
        help="Price shown in the draft. Always supplied by the caller from "
             "the dispatch pricing service — the AI never invents prices.")
    currency_id = fields.Many2one(
        "res.currency", string="Currency",
        default=lambda self: self.env.company.currency_id)
    price_reference = fields.Char(
        string="Price source", readonly=True,
        help="Where the price came from (e.g. the dispatch pricing service "
             "reference). Kept so every draft stays auditable.")
    facts_snapshot = fields.Json(
        string="Shipment facts used",
        help="Effective (newest) shipment facts with provenance, superseded "
             "candidates, and per-document sources this draft was built from.")
    generation_note = fields.Text(
        string="Generation note", readonly=True,
        help="Warnings/fallbacks from AI generation, if any.")

    # ── prepare ────────────────────────────────────────────────────────────

    @api.model
    def prepare_from_lead(self, lead, price_amount, currency=None,
                          price_reference="", extractor=None):
        """Generate and persist one editable NON-BINDING draft reply.

        ``lead``            — crm.lead recordset (exactly one).
        ``price_amount``    — required; the estimate price to SHOW. It comes
                              ONLY from the caller (dispatch PricingService /
                              BookingOrchestrationService); the engine never
                              invents prices and raises if it is omitted.
        ``currency``        — res.currency record (default: company currency).
        ``price_reference`` — free-text provenance for the price.
        ``extractor``       — optional callable overriding the default
                              shipment-fact extractor (tests inject fakes).

        Returns the created ``premafirm.lead.estimate.reply`` record.  No
        mail.mail, no booking, no confirmation, no state change — by design.
        """
        from odoo.addons.premafirm_ai_engine.services.lead_fact_service import (  # noqa: E501
            LeadFactService,
        )

        lead = lead.with_context(active_test=False)
        if not lead or len(lead) != 1:
            raise UserError("Prepare the estimate reply for exactly one opportunity.")
        if price_amount is None:
            raise UserError(
                "A price amount is required to prepare the draft — the engine "
                "never invents prices. Price it via the dispatch pricing "
                "service first (see docs/A2_CROSS_MODULE_CONTRACT.md).")
        facts = LeadFactService(self.env).extract_effective_facts(
            lead, extractor=extractor)

        def _clean(fact):
            fact = dict(fact)
            fact["at"] = _iso_naive_utc(fact.get("at"))
            return fact

        effective = {field: _clean(fact)
                     for field, fact in (facts.get("effective") or {}).items()}
        superseded = [_clean(fact) for fact in (facts.get("superseded") or [])]
        snapshot = {
            "effective": effective,
            "superseded": superseded,
            "sources": [{"kind": d.get("kind"), "source": d.get("source"),
                         "at": _iso_naive_utc(d.get("at"))}
                        for d in (facts.get("docs") or [])],
            "warnings": facts.get("warnings") or [],
        }
        fact_lines = [_fact_line(f) for f in sorted(effective.values(),
                                                    key=lambda f: f.get("field") or "")]

        currency = currency or self.env.company.currency_id
        body_paragraphs, note = self._generate_prose(lead, fact_lines)
        body_html = self._compose_body(
            body_paragraphs, price_amount, currency, price_reference, fact_lines)
        subject = self._compose_subject(lead)

        partner = lead.partner_id if lead.partner_id else False
        draft = self.create({
            "crm_lead_id": lead.id,
            "partner_id": partner.id if partner else False,
            "subject": subject,
            "body_html": body_html,
            "price_amount": price_amount,
            "currency_id": currency.id,
            "price_reference": price_reference or "",
            "facts_snapshot": snapshot,
            "generation_note": note or "",
        })
        return draft

    def _generate_prose(self, lead, fact_lines):
        """Ask the LLM for plain paragraphs ONLY — no price, no disclaimer.

        Returns (paragraphs, note).  Falls back to a factual summary when the
        AI is unavailable so the draft action never dies.
        """
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import (  # noqa: E501
            deepseek_chat, get_api_key, get_model, today_context_line,
        )

        context = today_context_line()
        facts_text = "\n".join("- %s" % line for line in fact_lines) or \
            "(no shipment details extracted — confirm details with the customer)"
        system = (
            "You draft the BODY of a short, professional PRELIMINARY reply "
            "email to a logistics customer. Context: %s\n"
            "Rules:\n"
            "- Use ONLY the confirmed shipment details supplied. Never invent "
            "details, times, addresses or prices.\n"
            "- Do NOT mention prices, rates, money or quotations at all: a "
            "price line and a non-binding disclaimer are appended by the "
            "system after you.\n"
            "- Do NOT promise binding commitments; this is a preliminary "
            "reply and a formal rate confirmation will follow.\n"
            "- Plain text paragraphs only, separated by blank lines, no "
            "headings, no markdown, no bullet lists, under 130 words.\n"
            "- Acknowledge the shipment and say you will follow up with the "
            "formal confirmation shortly.\n"
            "Confirmed shipment details:\n%s"
        ) % (context, facts_text)
        try:
            payload = deepseek_chat(
                [{"role": "user", "content": "Draft the reply body now."}],
                system=system, model=get_model(self.env),
                api_key=get_api_key(self.env), max_tokens=700, timeout=90)
        except Exception as e:  # never kill the draft action on AI failure
            _logger.warning("estimate-reply prose generation failed: %s", e)
            payload = ""
        text = ""
        if isinstance(payload, dict):
            text = payload.get("content") or ""
        elif isinstance(payload, str):
            text = payload
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip())
                      if p.strip()]
        if paragraphs:
            return paragraphs, ""
        note = ("AI prose generation unavailable or empty — factual fallback "
                "used. Review and rephrase before any use.")
        fallback = [
            "Thank you for your request.",
            "We are preparing the formal Customer Rate Confirmation for this "
            "shipment and will send it to you shortly.",
        ]
        return fallback, note

    def _compose_body(self, paragraphs, price_amount, currency, price_reference,
                      fact_lines):
        """Programmatic assembly: prose + price line + disclaimer.

        The price and the non-binding disclaimer come from parameters/code,
        never from the LLM, so the AI cannot invent figures.
        """
        price_label = currency.symbol or currency.name or ""
        price_text = ("%s %s" % (price_label, format(price_amount, ",.2f"))).strip()
        if price_reference:
            price_text += " — %s" % price_reference
        price_line = "Estimated price: %s" % price_text
        blocks = []
        for p in paragraphs:
            blocks.append("<p>%s</p>" % html.escape(p))
        blocks.append("<p><strong>%s</strong></p>" % html.escape(price_line))
        blocks.append("<p><em>%s</em></p>" % html.escape(NON_BINDING_NOTE))
        if fact_lines:
            details = "".join(
                "<li>%s</li>" % html.escape(line) for line in fact_lines)
            blocks.append(
                "<p style='margin-top:12px'><strong>Shipment details used "
                "for this draft (latest customer statements):</strong></p>"
                "<ul>%s</ul>" % details)
        return "".join(blocks)

    def _compose_subject(self, lead):
        base = lead.name or "Shipment opportunity"
        name = base.strip()
        if len(name) > 70:
            name = name[:67] + "..."
        return "Preliminary estimate reply — %s" % name


class CrmLeadEstimateReplyLink(models.Model):
    """Engine-local link surface so prepared drafts are discoverable on the
    opportunity.  The dispatch-side companion wires the two staff actions to
    this (docs/A2_CROSS_MODULE_CONTRACT.md)."""

    _inherit = "crm.lead"

    x_estimate_reply_ids = fields.One2many(
        "premafirm.lead.estimate.reply", "crm_lead_id",
        string="Preliminary estimate drafts")

    def action_open_estimate_replies(self):
        """Window action listing this opportunity's prepared draft replies."""
        self.ensure_one()
        return {
            "name": "Preliminary Estimate Drafts",
            "type": "ir.actions.act_window",
            "res_model": "premafirm.lead.estimate.reply",
            "view_mode": "list,form",
            "domain": [("crm_lead_id", "=", self.id)],
            "context": {"default_crm_lead_id": self.id},
            "target": "current",
        }
