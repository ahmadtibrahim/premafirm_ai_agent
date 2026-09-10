import json
import logging
import re

from odoo import api, fields, models, exceptions

from ..services.freight_display import (
    SERVICE_HEADER, build_operational_details, build_service_note,
    normalized_service_type, shipment_region, shipment_temp_kind,
)

_logger = logging.getLogger(__name__)


def _names_equivalent(name_a, name_b):
    """Fuzzy company-name equality for the customer sanity check.

    Normalizes apostrophes/punctuation and compares on the normalized
    prefixes, so "Baxter`s Bakery" (Odoo partner) and "Baxter's Bakery
    (Cobourg) Inc" (documents) are not flagged as a conflict.
    """
    def _norm(name):
        import re
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())

    norm_a, norm_b = _norm(name_a), _norm(name_b)
    if not norm_a or not norm_b:
        return False
    return (
        norm_a.startswith(norm_b)
        or norm_b.startswith(norm_a)
        or norm_a in norm_b
        or norm_b in norm_a
    )


# Legacy single-block markers — emitted only by the old quote builder
# (_quote_line_description, removed). The invoice-style layout never puts
# these on a product line, so their presence means the line still carries
# pre-layout AI text that must be migrated to the product + note layout.
_LEGACY_BLOCK_MARKERS = (
    "\nShipper:", "\nPickup:", "\nPickup date:", "\nDelivery:",
    "\nDelivery date:", "\nLoad:", "\nRefs:", "\nTemperature:",
    "\nEquipment:", "\nAccessorials:", "\nNotes:",
)


def _is_legacy_ai_block(name):
    """True when a line name is the old single-block AI description (before
    the product + note layout) or its bare-header fallback."""
    name = (name or "").strip()
    if not name:
        return False
    if name == SERVICE_HEADER:
        return True
    return any(marker in name for marker in _LEGACY_BLOCK_MARKERS)


def _product_label_set(product):
    """{name, display_name} of a product — used to recognize the untouched
    product-default line label (the label Odoo auto-fills when the product
    is placed on the line)."""
    labels = set()
    if not product:
        return labels
    for candidate in (product.name, product.display_name):
        candidate = (candidate or "").strip()
        if candidate:
            labels.add(candidate)
    return labels


def _quote_fallback_summary(result, price=None):
    """Deterministic one-line operational summary when the model returns no
    ai_summary — built only from facts actually extracted."""
    parts = []
    route = " → ".join(p for p in (
        (result.get("pickup_city") or "").strip(),
        (result.get("delivery_city") or "").strip(),
    ) if p)
    if route:
        parts.append(f"Route: {route}")
    try:
        pallets = int(float(result.get("pallets") or 0))
    except (TypeError, ValueError):
        pallets = 0
    try:
        weight = float(result.get("weight") or 0.0)
    except (TypeError, ValueError):
        weight = 0.0
    metrics = []
    if pallets > 0:
        metrics.append(f"{pallets} pallets")
    if weight > 0:
        metrics.append(f"{weight:,.0f} {(result.get('weight_unit') or 'lbs')}")
    commodity = (result.get("commodity") or "").strip()
    if metrics:
        parts.append("Load: " + ", ".join(metrics) + (f" — {commodity}" if commodity else ""))
    elif commodity:
        parts.append(f"Commodity: {commodity}")

    money = []
    if price is not None:
        money.append(f"${price:,.2f} {(result.get('currency') or 'CAD')}")
    tax_rate = result.get("tax_rate")
    tax_type = (result.get("tax_type") or "").strip()
    if tax_rate:
        rate_txt = f"{float(tax_rate):g}%" if tax_type else f"{float(tax_rate):g}%"
        money.append(f"+ {rate_txt} {tax_type}".strip())
    total = result.get("total_amount")
    if total:
        try:
            money.append(f"= ${float(total):,.2f} total")
        except (TypeError, ValueError):
            pass
    elif price is not None and tax_rate:
        try:
            money.append(f"= ${float(price) * (1 + float(tax_rate) / 100):,.2f} incl. tax")
        except (TypeError, ValueError):
            pass
    if money:
        parts.append("Rate: " + " ".join(money))
    return " | ".join(parts) or "Load details filled from the attached documents."


class SaleOrder(models.Model):
    _inherit = "sale.order"

    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")
    pickup_city = fields.Char()
    delivery_city = fields.Char()
    load_reference = fields.Char()

    # Re-Negotiation
    x_reneg_status = fields.Selection([
        ("none", "Normal"),
        ("requested", "Re-Negotiation Requested"),
        ("counter_sent", "Counter Offer Sent"),
    ], string="Re-Negotiation Status", default="none", required=True, tracking=True)
    x_reneg_customer_note = fields.Text("Customer Note")
    x_reneg_internal_note = fields.Text("Internal Notes")
    x_reneg_proposed_price = fields.Float("Counter Offer Price", digits=(16, 2))
    x_reneg_validity_date = fields.Date("Counter Offer Valid Until")
    x_reneg_counter_sent_at = fields.Datetime("Counter Offer Sent At", readonly=True)

    # ── AI Generate for Quotations ────────────────────────────
    x_ai_summary = fields.Text("AI Summary", copy=False)
    x_ai_summary_instruction = fields.Char(
        "Describe the load / Tell AI what to generate",
        copy=False,
        help="Optional: type a plain-English load description (e.g. 'Reefer delivery Toronto→Montreal, 24 pallets, liftgate'). AI Generate also inspects every attached document (rate confirmation, shipment details, instructions) automatically — pasted text and attachments are merged into one load record.",
    )
    x_ai_summary_at = fields.Datetime("Summary Generated At", readonly=True, copy=False)

    def action_ai_generate_quote(self):
        """AI Generate on a draft quotation — mirror of the working invoice flow.

        Inspects ALL attachments linked to the quotation (form + chatter) and
        reviews them TOGETHER with any pasted 'Describe the load' text,
        combining everything into ONE load record (deepseek call goes through
        the shared InvoiceAIService, exactly like action_ai_generate_invoice).
        The result populates the PremaFirm Load Details fields, the freight
        order line (rate + tax so the quotation total reconciles with the
        rate confirmation) and the AI Summary. Pasting a description is
        optional when valid attachments exist; when both exist they are
        merged. Existing good values are never overwritten silently —
        conflicts are surfaced on the chatter.
        """
        self.ensure_one()
        from ..services.invoice_ai_service import InvoiceAIService

        instruction = (self.x_ai_summary_instruction or "").strip()
        attachments = self._get_order_attachments()
        if not attachments and not instruction:
            raise exceptions.UserError(
                "No attachments were found on this quotation and no load "
                "description was pasted.\n"
                "Attach the rate confirmation / shipment details / pickup or "
                "delivery instructions, or type a description in the "
                "'Describe the load' field, then click AI Generate."
            )

        try:
            service = InvoiceAIService(self.env)
            result = service.analyze_quote(self, pasted_text=instruction)
        except ValueError as exc:
            raise exceptions.UserError(str(exc))
        except Exception as exc:
            _logger.exception("AI Generate Quote failed for %s", self.name)
            raise exceptions.UserError(
                f"AI generation failed: {type(exc).__name__}: {exc}"
            )

        if not result:
            raise exceptions.UserError(
                "AI returned no usable result. Please check the attachments and try again."
            )

        # The apply writes run under ai_apply_flow: the feedback hooks ignore
        # them, so AI Generate itself is never learned as a "human edit".
        return self.with_context(ai_apply_flow=True)._apply_quote_ai_result(
            result, attachments.ids or []
        )

    def _apply_quote_ai_result(self, result, attachment_ids=None):
        """Apply the structured load record returned by analyze_quote().

        Mirrors the invoice AI write flow and honours the SHARED feedback
        engine (premafirm.ai.baseline / premafirm.ai.feedback — same engine
        used by invoice AI Generate).

        Ownership ladder — MANUAL USER VALUE > AI EXTRACTION:
        * Header fields (PO#, BOL#, load reference, cities...) are filled when
          empty. A populated field is updated ONLY while it still equals the
          last AI baseline (AI wrote it and the user never touched it); a
          value the user corrected (differs from the baseline) or a value AI
          never wrote is preserved silently. AI populates data — it never
          takes ownership back from the user.
        * The freight line: same rule for product and price. The untouched
          product-default placeholder (e.g. the seeded "$1.00 (Reefer FTL)"
          stub) and pre-layout single-block AI descriptions are replaced by
          the product the shipment context calls for (LTL vs FTL, Canada vs
          US, Reefer vs Dry) — a product the user deliberately chose (custom
          line text) is preserved.
        * Layout parity with the invoice AI flow: the customer-facing text
          is ONE separate "Freight / Delivery Service" note line below the
          product line (shared freight_display formatter) — never crammed
          into the product-line description. Reference numbers and
          operational machinery stay in the dedicated header fields, the AI
          baseline and the internal Details block of the AI summary.
        * At the end a baseline snapshot of exactly what AI still owns is
          saved (JSON: generated_at + source_attachment_ids + the AI-written
          values), so later HUMAN edits of those values are detected and
          learned as corrections. Reruns never duplicate feedback: the whole
          apply runs under context ai_apply_flow and is invisible to the
          feedback hooks.
        * Conflicts worth a human eye (documents vs each other, tax notes)
          are still posted to the chatter; preserved user values are not
          flagged as noise anymore.
        """
        conflicts = []
        doc_conflicts = (result.get("conflict_notes") or "").strip()
        if doc_conflicts:
            conflicts.append(f"Document conflict: {doc_conflicts}")

        # Customer name sanity check (documents vs quotation partner)
        extracted_customer = (result.get("customer") or "").strip()
        if extracted_customer and self.partner_id and not _names_equivalent(
            extracted_customer, self.partner_id.name or ""
        ):
            conflicts.append(
                f"Documents name the customer as '{extracted_customer}' but this "
                f"quotation is for {self.partner_id.name} — confirm the partner "
                "before sending."
            )

        # ── What did the previous AI run write? (empty dict on first run) ──
        from .ai_feedback import _values_equal
        baseline_model = self.env["premafirm.ai.baseline"]
        old_payload = baseline_model.get_dict("sale.order", self.id)

        # ── Load-detail header fields ──────────────────────────────────────
        order_vals = {}
        header_owned = {}  # field_name → value AI owns after this run
        for field_name, value, label in (
            ("premafirm_po", result.get("po_number"), "PO #"),
            ("premafirm_bol", result.get("bol_number"), "BOL #"),
            ("load_reference", result.get("load_reference"), "Load reference"),
            ("client_order_ref", result.get("customer_reference"), "Customer reference"),
            ("pickup_city", result.get("pickup_city"), "Pickup city"),
            ("delivery_city", result.get("delivery_city"), "Delivery city"),
        ):
            new_value = str(value).strip() if value not in (None, "") else ""
            current = self[field_name]
            current_s = str(current or "").strip()
            ai_owned_before = (
                field_name in old_payload
                and _values_equal(old_payload[field_name], current)
            )
            if ai_owned_before:
                # AI wrote it, user never changed it → still AI-owned.
                if new_value and new_value != current_s:
                    order_vals[field_name] = new_value  # refreshed extraction
                header_owned[field_name] = new_value or current_s
            elif not current_s and new_value:
                # Unpopulated → AI fills it and takes ownership.
                order_vals[field_name] = new_value
                header_owned[field_name] = new_value
            # else: manual/user value (or AI never wrote it) → kept silently.
        if order_vals:
            self.write(order_vals)

        # ── Freight order line (rate + tax from the rate confirmation) ──
        # Invoice-parity layout — ONE shared standard (freight_display):
        #   * ONE product line carrying the contextually-resolved product
        #     and the rate;
        #   * ONE separate "Freight / Delivery Service" note line below it
        #     with the clean visible block (Route / Date / Pickup / Delivery
        #     / Load / Commodity / Temperature / short Note).
        # Reference numbers (PO/BOL/load ref) and operational machinery
        # (contacts, booking URLs, phones, DC emails…) never reach line
        # names: they stay in the dedicated header fields, the AI baseline
        # and the internal Details block of the AI summary below.
        product = self._resolve_quote_product(result)
        price = self._quote_rate_price(result)
        tax_ids, tax_note = self._quote_tax_resolution(result, product)
        if tax_note:
            conflicts.append(tax_note)
        service_note = build_service_note(result)  # '' → no note line

        product_lines = self.order_line.filtered(lambda l: not l.display_type)
        note_lines = self.order_line.filtered(
            lambda l: l.display_type == "line_note")
        ai_notes = note_lines.filtered(
            lambda n: (n.name or "").startswith(SERVICE_HEADER))

        line_owned = {}  # 'line:<id>:<field>' → value AI owns after this run
        primary = product_lines.sorted("sequence")[0] if product_lines else False

        if primary:
            update = {}
            current_name = (primary.name or "").strip()
            current_product = primary.product_id
            product_key = f"line:{primary.id}:product_id"
            price_key = f"line:{primary.id}:price_unit"
            legacy_line = _is_legacy_ai_block(current_name)

            # A pre-layout single-block AI description on the product line is
            # replaced by the product's own label — the visible service text
            # now lives on the note line below.
            if product and legacy_line and product.display_name:
                update["name"] = product.display_name

            # ── Product (resolved from the shipment context) ───────────────
            product_owned = False
            if product and current_product.id != product.id:
                ai_owned_product = (
                    product_key in old_payload
                    and str(old_payload[product_key]) == str(current_product.id)
                )
                # Swap the product only when the line is AI's to refresh:
                # AI-owned from a previous run, pre-baseline single-block AI
                # text, or the untouched product-default placeholder (a
                # nominal-price product with the auto label, e.g. the seeded
                # "$1.00 (Reefer FTL)" stub). A product the user deliberately
                # chose (custom line text) is preserved silently.
                untouched_default = bool(current_product) and (
                    (current_product.list_price or 0.0) <= 1.0
                    and (not current_name
                         or current_name in _product_label_set(current_product))
                )
                if ai_owned_product or legacy_line or untouched_default:
                    update["product_id"] = product.id
                    product_owned = True
            elif product and current_product.id == product.id:
                product_owned = True

            # ── Price (rate): fill / refresh while AI-owned, else keep ─────
            current_price = primary.price_unit or 0.0
            default_price = (product.list_price or 0.0) if product else 0.0
            unedited_default = bool(product) and abs(current_price - default_price) < 0.005
            ai_owns_price = (
                price_key in old_payload
                and _values_equal(old_payload[price_key], current_price)
            )
            price_owned = False
            if ai_owns_price:
                # AI price the user never touched → refresh when re-extracted.
                if price is not None and not _values_equal(
                        old_payload[price_key], price):
                    update["price_unit"] = price
                price_owned = True
            elif not current_price or unedited_default:
                # Unpriced / untouched product-default stub → fill with rate.
                if price is not None:
                    update["price_unit"] = price
                    price_owned = True
            # else: a real amount already on the line → kept silently.
            if update and tax_ids is not None:
                update["tax_id"] = [(6, 0, tax_ids)]
            if update:
                primary.write(update)

            # ── Ownership after this run (feeds the new baseline) ─────────
            if product_owned and primary.product_id:
                line_owned[product_key] = str(primary.product_id.id)
            if price_owned and primary.price_unit:
                line_owned[price_key] = primary.price_unit
        else:
            # No freight line yet → create the product line; the note line is
            # added right after it (same branch as an AI-owned note below).
            line = self.env["sale.order.line"].create({
                "order_id": self.id,
                "product_id": product.id if product else False,
                "name": (product.display_name
                         if product and product.display_name
                         else SERVICE_HEADER),
                "product_uom_qty": 1,
                "price_unit": price or 0,
            })
            if tax_ids is not None:
                line.write({"tax_id": [(6, 0, tax_ids)]})
            if product:
                line_owned[f"line:{line.id}:product_id"] = str(product.id)
            if line.price_unit:
                line_owned[f"line:{line.id}:price_unit"] = line.price_unit
            product_lines = self.order_line.filtered(lambda l: not l.display_type)

        # ── "Freight / Delivery Service" note line (invoice parity) ────────
        def _note_owned(note):
            key = f"line:{note.id}:name"
            return (key in old_payload
                    and _values_equal(old_payload[key], note.name or ""))

        if ai_notes:
            note = ai_notes[0]
            # Drop duplicate AI notes (double-clicks / earlier layout runs) —
            # only while AI still owns them or they carry the current text; a
            # duplicate a human rewrote is preserved.
            dupes = ai_notes[1:].filtered(
                lambda d: _note_owned(d)
                or (service_note and (d.name or "").strip() == service_note))
            if dupes:
                dupes.unlink()
            note_key = f"line:{note.id}:name"
            if service_note:
                if (note.name or "").strip() == service_note:
                    line_owned[note_key] = (note.name or "").strip()
                elif _note_owned(note):
                    note.write({"name": service_note})
                    line_owned[note_key] = (note.name or "").strip()
                # else: note text a human changed → preserved silently; AI
                # never restores over it and the change is learned as
                # feedback through the note's baseline key.
            elif _note_owned(note):
                # No factual block this run → an AI-owned note is removed
                # (a human-edited one is kept).
                note.unlink()
        elif service_note and product_lines:
            last_product = product_lines.sorted("sequence")[-1]
            note = self.env["sale.order.line"].create({
                "order_id": self.id,
                "display_type": "line_note",
                "name": service_note,
                "sequence": (last_product.sequence or 100) + 1,
            })
            line_owned[f"line:{note.id}:name"] = (note.name or "").strip()

        # ── AI baseline snapshot (internal metadata — never shown in UI) ───
        try:
            payload = {}
            for field_name, value in header_owned.items():
                value_s = str(value or "").strip()
                if value_s:
                    payload[field_name] = value_s
            payload.update(line_owned)
            payload["generated_at"] = fields.Datetime.now().isoformat()
            baseline_model.set_for(
                "sale.order", self.id, payload, partner=self.partner_id,
                created_by=self.env.user, attachment_ids=attachment_ids or [],
            )
            _logger.info("AI baseline saved for %s (%d value(s), %d attachment(s))",
                         self.name, len(payload), len(attachment_ids or []))
        except Exception:
            _logger.exception("Failed to save AI baseline for %s", self.name)

        # ── AI Summary (regenerated like the invoice flow) ────────────────
        # Operational detail that is deliberately NOT on the customer-facing
        # lines (contacts, booking URL/emails/phones, requested equipment,
        # full instructions…) is preserved here — marked internal so it is
        # never mistaken for customer text.
        summary = (result.get("ai_summary") or "").strip()
        if not summary:
            summary = _quote_fallback_summary(result, price)
        details = build_operational_details(result)
        if details:
            summary = (f"{summary}\n\nDetails (internal):\n{details}"
                       if summary else f"Details (internal):\n{details}")
        self.write({
            "x_ai_summary": summary,
            "x_ai_summary_at": fields.Datetime.now(),
            "x_ai_summary_instruction": False,
        })

        # ── Surface conflicts / review notes on the chatter ───────────────
        if conflicts:
            self.message_post(
                body="<b>AI Generate</b> — review notes:<br/>"
                     + "<br/>".join(f"• {note}" for note in conflicts)
            )

        confidence = result.get("confidence", "unknown")
        source = (
            f"{len(attachment_ids or [])} attachment(s)"
            if attachment_ids else "pasted description"
        )
        message = (
            f"Quotation populated from {source} ({confidence} confidence): "
            f"load details, freight line and AI summary updated."
        )
        if conflicts:
            message += f" {len(conflicts)} review note(s) posted to the chatter."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Generate Complete",
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }


    def _resolve_quote_product(self, result):
        """Freight product for the quotation line — shipment context first,
        never a one-size default.

        The context is LTL vs FTL + Canada vs US + Reefer vs Dry (NOT the
        trailer-size word on a stub product such as "Reefer 53ft"):
          1. with a service-type signal, catalog products score on service
             type (+30), lane region (+12), temperature kind (+6/−6) and the
             best score wins;
          2. no usable context → the AI's own product_id choice;
          3. → the catalog product for the extracted service type;
          4. → the first generic freight/transport service product.
        """
        product = False
        service_type = normalized_service_type(result)
        # Deterministic guard: a small pallet/weight load with NO explicit
        # full-truckload wording on the documents is LTL — the trailer word
        # ("Reefer 53ft") describes equipment, never the service. Overrides an
        # LLM misread of a partial load as 'ftl' (truckload_wording is the
        # verbatim wording extracted by the shared classification rule).
        if service_type == "ftl":
            wording = str(result.get("truckload_wording") or "").strip()
            if not wording:
                try:
                    pallets = int(float(result.get("pallets") or 0))
                except (TypeError, ValueError):
                    pallets = 0
                if 0 < pallets <= 15:
                    service_type = "ltl"
        context_st = (
            service_type if service_type in ("ltl", "ftl", "local") else ""
        )
        if context_st:
            region = shipment_region(
                result, (self.partner_id.country_id.code or ""))
            temp_kind = shipment_temp_kind(result)
            rows = self.env["premafirm.invoice.ai.product"].sudo().search([
                ("ai_enabled", "=", True),
            ])
            # Catalog rows decide eligibility (the generic "Freight Service"
            # product is a consu row) — accessorial-only rows never win the
            # freight-product slot.
            rows = [r for r in rows
                    if r.product_id.exists()
                    and r.product_id.active
                    and "liftgate" not in
                    (r.product_id.display_name or "").lower()]

            def _score(row):
                text = " ".join((
                    row.product_id.display_name or "",
                    row.description_hint or "",
                )).lower()
                score = 0
                if context_st == "ltl" and re.search(r"\bltl\b", text):
                    score += 30
                elif context_st == "ftl" and re.search(r"\bftl\b", text):
                    score += 30
                elif context_st == "local" and re.search(r"\blocal\b", text) \
                        and "gta" in text:
                    score += 30  # the local-GTA product
                if region == "ca":
                    if re.search(r"canada", text):
                        score += 12
                    elif re.search(r"\busa\b|united states", text):
                        score -= 10
                elif region == "us":
                    if re.search(r"\busa\b|united states", text):
                        score += 12
                    elif re.search(r"canada", text):
                        score -= 10
                reefer_words = ("reefer", "frozen", "chilled", "chill")
                has_reefer = any(w in text for w in reefer_words)
                if temp_kind == "reefer":
                    score += 6 if has_reefer else -6
                elif temp_kind == "dry" and has_reefer:
                    score -= 6
                return score

            scored = sorted(rows, key=lambda r: (-_score(r), r.id))
            if scored and _score(scored[0]) > 0:
                product = scored[0].product_id

        if not product:
            # No usable shipment context → trust the AI's catalog pick.
            product_id = result.get("product_id")
            if product_id:
                try:
                    product = self.env["product.product"].sudo().browse(
                        int(product_id)
                    ).exists()
                except (TypeError, ValueError):
                    product = False
        if not product:
            catalog = self.env["premafirm.invoice.ai.product"].sudo().search([
                ("ai_enabled", "=", True),
                ("service_type", "=", service_type or "other"),
            ], order="id", limit=1)
            if catalog and catalog.product_id.exists():
                product = catalog.product_id
        if not product:
            product = self.env["product.product"].sudo().search([
                ("type", "in", ("service", "consu")), ("active", "=", True),
                "|", ("name", "ilike", "freight"), ("name", "ilike", "transport"),
            ], order="id", limit=1)
        if not product:
            return False
        return product

    def _quote_rate_price(self, result):
        """Price basis for the freight line from the rate-confirmation money.

        subtotal → freight (+ separately charged accessorials) → grand total
        (when that is the only amount shown). Returns a float or None when the
        documents carry no money at all.
        """
        def _num(key):
            val = result.get(key)
            if val in (None, "", "null", 0):
                return None
            try:
                val = float(str(val).replace(",", "").replace("$", "").strip())
            except (TypeError, ValueError):
                return None
            return val if val else None

        freight = _num("freight_amount")
        accessorial = _num("accessorial_amount")
        subtotal = _num("subtotal_amount")
        total = _num("total_amount")
        all_in = bool(result.get("rate_includes_tax"))

        if all_in and freight is None and subtotal is None:
            return total  # single all-in amount — taxed at the line level below
        if subtotal is not None:
            return subtotal
        if freight is not None or accessorial is not None:
            return (freight or 0.0) + (accessorial or 0.0)
        return total

    def _quote_tax_resolution(self, result, product):
        """Taxes to force on the freight line, mirroring the invoice flow.

        Returns (tax_ids | None, note):
        • None          → leave the product's default taxes untouched (the
                          rate confirmation's tax matches the product default,
                          or no tax detail is available);
        • []            → no tax on the line (rate confirmation shows no tax,
                          or the quoted amount is all-in including tax);
        • [tax.id]      → the document shows a tax rate that does not match the
                          product defaults and a matching company sale tax
                          exists.
        """
        if not product:
            return None, ""
        all_in = bool(result.get("rate_includes_tax"))
        mentioned = bool(result.get("tax_mentioned"))
        tax_amount = result.get("tax_amount")
        try:
            tax_rate = float(result.get("tax_rate") or 0.0)
        except (TypeError, ValueError):
            tax_rate = 0.0

        if all_in:
            return [], (
                "Rate confirmation shows one all-in amount (tax included) — "
                "the freight line was quoted without an added tax line; "
                "confirm the tax treatment before invoicing."
            )
        if not mentioned:
            # No tax wording found in the documents — quote tax-free, exactly
            # like the invoice flow clears taxes when tax is not mentioned.
            return [], ""
        if tax_rate:
            defaults = product.taxes_id
            if any(
                t.amount_type == "percent" and abs(t.amount - tax_rate) < 0.005
                for t in defaults
            ):
                return None, ""
            tax = self.env["account.tax"].sudo().search([
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", "percent"),
                ("amount", "=", tax_rate),
                "|", ("company_id", "=", self.company_id.id),
                ("company_id", "=", False),
            ], limit=1)
            if tax:
                return [tax.id], ""
            return None, (
                f"Rate confirmation shows {tax_rate:g}% tax, which does not "
                "match the product's default taxes — verify the tax on the "
                "order line before sending."
            )
        return None, ""

    # ── Confirmation-email safety (S00094 guard) ──────────────────────
    # Marker fields for the idempotent "Send Confirmation Email" action.
    # Once set, the order-confirmation email can never be sent again.
    x_confirmation_email_sent_at = fields.Datetime(
        "Confirmation Email Sent", readonly=True, copy=False, tracking=True)
    x_confirmation_email_template_id = fields.Many2one(
        "mail.template", "Confirmation Email Template", readonly=True, copy=False)

    def action_request_renegotiation(self):
        self.ensure_one()
        self.x_reneg_status = "requested"

    def action_send_counter_offer(self):
        self.ensure_one()
        self.write({
            "x_reneg_status": "counter_sent",
            "x_reneg_counter_sent_at": fields.Datetime.now(),
        })
        return self.action_quotation_send()

    def action_confirm(self):
        # Plain passthrough: confirmation itself must never email anyone.
        # The implicit email is suppressed in _send_order_notification_mail below
        # (S00094 guard), so this method stays a pure state transition.
        return super().action_confirm()

    def _send_order_notification_mail(self, mail_template):
        """S00094 guard — the confirmation email is only ever sent on an EXPLICIT ask.

        sale_management.action_confirm auto-sends the quotation template's mail on
        EVERY backend Confirm (even with no ``send_email`` context), and again on
        every cancel → draft → re-confirm cycle. That silent path produced the
        duplicated Rate Confirmation emails on S00093/S00094 (Link Street).

        New rule — this method mails ONLY when the caller explicitly asked:
          * context ``send_email``            → the CUSTOMER's own explicit action
            (portal payment / digital quote sign-off / online approval);
          * context ``premafirm_send_order_mail`` → staff's explicit
            "Send Confirmation Email" button (action_send_order_confirmation).

        Everything else (backend Confirm button, AI / automation / cron / XML-RPC
        confirms) is a silent no-op here. A per-order marker makes the send
        idempotent: the confirmation email can never go out twice for an order.
        """
        if not (self.env.context.get('send_email')
                or self.env.context.get('premafirm_send_order_mail')):
            return  # silent no-op — confirming an order never emails anyone
        self.ensure_one()
        if not mail_template:
            return
        # Row lock + marker BEFORE the send: two near-simultaneous clicks cannot
        # both pass the check, and a failed send rolls the marker back with the tx.
        self.env.cr.execute(
            'SELECT 1 FROM sale_order WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.x_confirmation_email_sent_at:
            if self.env.context.get('premafirm_send_order_mail'):
                raise exceptions.UserError(
                    'The confirmation email for %s was already sent to the customer '
                    'on %s%s. No duplicate email was sent. If the customer needs '
                    'another copy, use "Send by Email" — that manual message is '
                    'never blocked by this guard.'
                    % (self.name,
                       self.x_confirmation_email_sent_at.strftime('%Y-%m-%d %H:%M'),
                       ' (%s)' % self.x_confirmation_email_template_id.name
                       if self.x_confirmation_email_template_id else ''))
            return  # portal/payment retry path: already mailed once — never again
        self.write({
            'x_confirmation_email_sent_at': fields.Datetime.now(),
            'x_confirmation_email_template_id': mail_template.id,
        })
        return super()._send_order_notification_mail(mail_template)

    def action_send_order_confirmation(self):
        """Explicit staff action: email the customer their Rate Confirmation.

        This is the ONLY backend path that may send the order-confirmation email.
        Idempotent — refuses to send twice (see x_confirmation_email_sent_at).
        """
        self.ensure_one()
        if self.state != 'sale':
            raise exceptions.UserError(
                'Only confirmed orders (state "Sale Order") can send a confirmation '
                'email. This order is in state "%s".' % self.state)
        template = self._get_confirmation_template()
        if not template:
            raise exceptions.UserError(
                'No confirmation email template is set for this order or its quotation '
                'template. Set one, or use "Send by Email" for a manual message.')
        self.with_context(
            premafirm_send_order_mail=True)._send_order_notification_mail(template)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        ref = self.premafirm_bol or self.premafirm_po or ""
        update = {
            "premafirm_po": self.premafirm_po or "",
            "premafirm_bol": self.premafirm_bol or "",
            "premafirm_pod": self.premafirm_pod or "",
            "load_reference": self.load_reference or "",
            "payment_reference": self.client_order_ref or "",
            "invoice_origin": self.name or "",
        }
        if ref:
            update["ref"] = ref
        vals.update(update)
        partner_country = self.partner_id.country_id.code
        usa_company = self.env["res.company"].search([("name", "ilike", "usa")], limit=1)
        canada_company = self.env["res.company"].search([("name", "ilike", "can")], limit=1)
        company = usa_company if partner_country == "US" and usa_company else canada_company if canada_company else self.company_id
        vals["company_id"] = company.id

        journal = self.env["account.journal"].search([
            ("type", "=", "sale"),
            ("company_id", "=", company.id),
            ("name", "ilike", "USA" if partner_country == "US" else "CAN"),
        ], limit=1)
        if journal:
            vals["journal_id"] = journal.id
        return vals

    # ── Customer deposit on quotation / order ────────────────────────────
    deposit_recorded = fields.Boolean(
        compute="_compute_deposit_recorded", string="Deposit Recorded",
        help="A down-payment (deposit) has already been recorded on this order.")

    @api.depends("order_line.is_downpayment", "order_line.display_type")
    def _compute_deposit_recorded(self):
        for order in self:
            # the core down-payment flow adds an is_downpayment section line
            # AND is_downpayment product line(s) to the order — either means a
            # deposit was recorded (sale.order.line product lines carry
            # display_type=False, NOT "product" — that value is account.move's)
            order.deposit_recorded = bool(order.order_line.filtered(
                lambda l: l.is_downpayment))

    def action_record_deposit(self):
        """'Customer Deposit' button: open the record-deposit wizard.

        The wizard creates + posts the down-payment invoice through the core
        sale.advance.payment.inv machinery and auto-applies any unmatched bank
        payment already received (INV/2026/00093 fix).
        """
        self.ensure_one()
        if self.state != "sale":
            raise exceptions.UserError(
                'Confirm the quotation first (state "Sale Order") before '
                "recording a customer deposit.")
        if self.deposit_recorded:
            raise exceptions.UserError(
                "A deposit was already recorded on %s — see its down-payment "
                "invoice on the Invoices smart button." % self.name)
        return {
            "type": "ir.actions.act_window",
            "name": "Record Customer Deposit",
            "res_model": "premafirm.sale.deposit.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_sale_order_id": self.id},
        }

    def _get_order_attachments(self):
        """Attachments attached to this order that should follow onto its
        invoices: form/drop attachments (res_model='sale.order') plus chatter
        attachments (res_model='mail.message' on this order's messages).

        The auto-generated Quotation/Order PDF is excluded — it is a snapshot
        of the order itself, not a document the customer attached (the POD
        scans etc. on the order do follow, INV/2026/00093 fix).
        """
        self.ensure_one()
        sources = self.env["ir.attachment"].search([
            "|",
            "&", ("res_model", "=", "sale.order"), ("res_id", "=", self.id),
            "&", ("res_model", "=", "mail.message"),
                 ("res_id", "in", self.message_ids.ids),
        ])
        if not sources:
            return sources
        import re
        auto_pdf = re.compile(r"^(Quotation|Order) - .+\.pdf$")
        return sources.filtered(lambda a: not auto_pdf.match(a.name or ""))

    def _copy_attachments_to_invoices(self, moves):
        """Copy each order's attachments onto the invoice(s) created from it.

        Mirrors the manual drag-and-drop onto the invoice form
        (res_model='account.move'); skips names already present on the move so
        re-running never duplicates.
        """
        for move in moves:
            order = self.filtered(lambda o: o.name == move.invoice_origin)
            if not order and move.move_type == "out_invoice":
                order = move.line_ids.sale_line_ids.order_id[:1]  # DP invoices too
            if not order:
                continue
            sources = order._get_order_attachments()
            if not sources:
                continue
            existing = self.env["ir.attachment"].search([
                "|",
                "&", ("res_model", "=", "account.move"), ("res_id", "=", move.id),
                "&", ("res_model", "=", "mail.message"),
                     ("res_id", "in", move.message_ids.ids),
            ])
            existing_names = {a.name for a in existing}
            for att in sources:
                if att.name in existing_names:
                    continue
                att.copy({"res_model": "account.move", "res_id": move.id})
                existing_names.add(att.name)

    def _create_invoices(self, grouped=False, final=False, date=None):
        """Create invoice(s) for this order, then copy the order's attachments
        (POD scans, rate sheets…) onto the new invoice(s).

        This is the "Create Invoice" button from the quotation/order — before
        this override attachments attached to the order never travelled to the
        invoice (INV/2026/00093 fix).
        """
        moves = super()._create_invoices(grouped=grouped, final=final, date=date)
        if moves:
            self._copy_attachments_to_invoices(moves)
        return moves


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    scheduled_time = fields.Datetime(related="scheduled_date", store=True, readonly=False)
