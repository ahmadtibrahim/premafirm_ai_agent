import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
        if self.env.context.get("premafirm_preview_only"):
            raise UserError(_(
                "Preview is read-only. Return to the quotation and use "
                "Confirm Internally when you are ready."
            ))
        return super().action_confirm()

    def action_preview(self):
        """Keep Preview read-only even when synchronous automations exist."""
        state_before = {order.id: order.state for order in self}
        preview_self = self.with_context(premafirm_preview_only=True)
        result = super(SaleOrder, preview_self).action_preview()
        self.invalidate_recordset(['state'])
        changed = self.filtered(lambda order: order.state != state_before[order.id])
        if changed:
            raise UserError(_(
                "Preview was blocked because an automation tried to change "
                "the quotation state. No change was saved."
            ))
        return result

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


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    scheduled_time = fields.Datetime(related="scheduled_date", store=True, readonly=False)
