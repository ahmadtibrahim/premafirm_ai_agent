import json
import logging

from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


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
        help="Type a plain-English description (e.g. 'Reefer delivery Toronto→Montreal, 24 pallets, liftgate') then click AI Generate.",
    )
    x_ai_summary_at = fields.Datetime("Summary Generated At", readonly=True, copy=False)

    # ── Confirmation-email safety (S00094 guard) ──────────────────────
    # Marker fields for the idempotent "Send Confirmation Email" action.
    # Once set, the order-confirmation email can never be sent again.
    x_confirmation_email_sent_at = fields.Datetime(
        "Confirmation Email Sent", readonly=True, copy=False, tracking=True)
    x_confirmation_email_template_id = fields.Many2one(
        "mail.template", "Confirmation Email Template", readonly=True, copy=False)

    def action_ai_generate_quote(self):
        """AI Generate for sale.order — reads x_ai_summary_instruction and fills product + description."""
        self.ensure_one()
        from ..services.deepseek_utils import deepseek_chat, get_api_key as _get_deepseek_key, get_model as _get_deepseek_model
        api_key = _get_deepseek_key(self.env)
        if not api_key:
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "AI Generate", "message": "AI API key not configured.", "type": "warning"},
            }

        instruction = (self.x_ai_summary_instruction or "").strip()
        if not instruction:
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {
                    "title": "AI Generate",
                    "message": "Type a load description in the 'Describe the load' field first, then click AI Generate.",
                    "type": "warning", "sticky": False,
                },
            }

        partner_name = self.partner_id.name if self.partner_id else ""
        model = _get_deepseek_model(self.env)

        system = (
            "You are a freight quotation assistant at PremaFirm Inc., a Canadian trucking company. "
            "Given a plain-English load description, return ONLY a JSON object with these keys:\n"
            "  product_keyword: single word to search for the right service product (e.g. 'reefer', 'flatbed', 'freight')\n"
            "  service_name: short professional service line name for the quotation (max 80 chars)\n"
            "  service_description: 2-3 sentence professional description of the service, route, and key details\n"
            "  ai_summary: 2-3 sentence internal operational summary including route, commodity, and any risk notes\n"
            "Return ONLY the JSON. No markdown, no explanation."
        )
        user_msg = f"Load description: {instruction}\nCustomer: {partner_name or 'Unknown'}"

        try:
            raw = deepseek_chat(
                messages=[{"role": "user", "content": user_msg}],
                system=system, max_tokens=400, api_key=api_key, model=model, timeout=30,
            )
            # Robust JSON extraction
            parsed = None
            try:
                parsed = json.loads(raw.strip())
            except Exception:
                import re
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        pass
            if not parsed:
                raise ValueError(f"AI returned non-JSON: {raw[:200]}")
        except Exception as e:
            _logger.exception("AI Generate Quote failed for %s", self.name)
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "AI Generate Failed", "message": str(e)[:200], "type": "danger", "sticky": False},
            }

        # Find best matching product
        keyword = (parsed.get("product_keyword") or "freight").strip().lower()
        product = self.env["product.product"].sudo().search([
            ("type", "=", "service"), ("active", "=", True),
            ("name", "ilike", keyword),
        ], limit=1)
        if not product:
            product = self.env["product.product"].sudo().search([
                ("type", "=", "service"), ("active", "=", True),
                '|', ("name", "ilike", "freight"), ("name", "ilike", "transport"),
            ], limit=1)

        svc_name = (parsed.get("service_name") or "Freight Service").strip()[:80]
        svc_desc = (parsed.get("service_description") or "").strip()
        summary = (parsed.get("ai_summary") or "").strip()

        # Apply to existing draft lines if there's already one service product line
        existing = self.order_line.filtered(lambda l: l.display_type == "product" and l.product_id)
        if existing and product:
            existing[0].write({"product_id": product.id, "name": svc_name})
        elif existing:
            existing[0].write({"name": svc_name})
        elif product:
            self.order_line = [(0, 0, {
                "product_id": product.id,
                "name": svc_name,
                "product_uom_qty": 1,
                "price_unit": 0,
            })]

        # Add/update description note line
        note_lines = self.order_line.filtered(lambda l: l.display_type == "line_note")
        if note_lines and svc_desc:
            note_lines[0].write({"name": svc_desc})
        elif svc_desc:
            self.order_line = [(0, 0, {"display_type": "line_note", "name": svc_desc})]

        # Update AI summary + clear instruction
        self.write({
            "x_ai_summary": summary,
            "x_ai_summary_at": fields.Datetime.now(),
            "x_ai_summary_instruction": False,
        })

        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": "AI Generate Complete",
                "message": f"Quotation filled using '{keyword}' product.",
                "type": "success", "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

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
