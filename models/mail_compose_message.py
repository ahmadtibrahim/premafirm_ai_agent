import ast
import re

from odoo import api, models


class MailComposeMessage(models.TransientModel):
    _inherit = "mail.compose.message"

    @api.model
    def _extract_single_res_id(self, raw_res_ids):
        if not raw_res_ids:
            return None

        parsed = raw_res_ids
        if isinstance(raw_res_ids, str):
            try:
                parsed = ast.literal_eval(raw_res_ids)
            except (ValueError, SyntaxError):
                return None

        if isinstance(parsed, (list, tuple)) and len(parsed) == 1:
            try:
                return int(parsed[0])
            except (TypeError, ValueError):
                return None

        return None

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)

        body = defaults.get("body")
        model = defaults.get("model") or self.env.context.get("active_model")
        if body or model != "crm.lead":
            return defaults

        lead_id = None
        if self.env.context.get("active_model") == "crm.lead":
            lead_id = self.env.context.get("active_id")

        if not lead_id:
            lead_id = self._extract_single_res_id(defaults.get("res_ids"))

        if not lead_id:
            return defaults

        lead = self.env["crm.lead"].browse(lead_id).exists()
        if not lead:
            return defaults

        # Use dispatch-based draft if the lead has stops data; otherwise use GPT
        stop_ids = getattr(lead, "dispatch_stop_ids", [])
        if stop_ids:
            defaults["body"] = self._build_professional_draft(lead)
        else:
            defaults["body"] = self._build_gpt_draft(lead) or self._build_professional_draft(lead)
        return defaults

    @api.model
    def _build_gpt_draft(self, lead):
        """Generate an AI-drafted reply using full thread + account context."""
        try:
            api_key = (self.env['ir.config_parameter'].sudo().get_param('openai.api_key') or '').strip()
            if not api_key:
                return None

            # Build thread context
            import re as _re
            thread_lines = []
            for msg in lead.message_ids.sorted('date', reverse=True)[:8]:
                if msg.message_type not in ('email', 'comment'):
                    continue
                body = _re.sub(r'<[^>]+>', ' ', msg.body or '')
                body = _re.sub(r'\s+', ' ', body).strip()
                if not body or len(body) < 10:
                    continue
                direction = '→ SENT' if (msg.author_id and msg.author_id.user_ids) else '← RECEIVED'
                ts = msg.date.strftime('%Y-%m-%d') if msg.date else '?'
                thread_lines.append(f'[{direction} | {ts}]: {body[:400]}')

            if not thread_lines:
                return None  # No thread to base a draft on

            contact = lead.partner_id
            company = contact.parent_id if contact and contact.parent_id else (
                contact if contact and contact.is_company else None)
            contact_name = contact.name if contact else lead.partner_name or 'the contact'
            company_name = company.name if company else contact_name

            thread_text = '\n'.join(thread_lines)

            try:
                profile = self.env['premafirm.business.profile'].sudo().get_profile()
                tone = dict(profile._fields['tone_of_voice'].selection).get(
                    profile.tone_of_voice, 'professional')
            except Exception:
                tone = 'professional'

            from odoo.addons.premafirm_ai_engine.services.openai_utils import openai_chat
            draft_text = openai_chat(
                messages=[{
                    'role': 'user',
                    'content': (
                        f'Draft a reply email to {contact_name} at {company_name}.\n'
                        f'Tone: {tone}. Keep it under 120 words unless the thread requires more.\n'
                        f'Do NOT include a signature — the email client adds it automatically.\n\n'
                        f'Previous thread:\n{thread_text}'
                    ),
                }],
                system=(
                    'You are Ahmad Ibrahim\'s AI freight sales assistant at PremaFirm Inc., '
                    'a Canadian trucking carrier. Draft professional reply emails for freight sales outreach. '
                    'Be concise and relevant to the conversation. '
                    'Return ONLY the email body text — no subject line, no signature, no extra commentary.'
                ),
                max_tokens=400,
                api_key=api_key,
            )
            # Convert plain text to HTML
            html = '<br/>'.join(draft_text.replace('\r\n', '\n').split('\n'))
            return html
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug('GPT draft error: %s', exc)
            return None

    @api.model
    def _extract_city(self, address):
        if not address:
            return ""
        return (address.split(",", 1)[0] or "").strip()

    @api.model
    def _build_professional_draft(self, lead):
        # Use getattr throughout — these fields may not exist on all deployments
        stop_ids   = getattr(lead, "dispatch_stop_ids", [])
        pickups    = stop_ids.filtered(lambda s: s.stop_type == "pickup") if stop_ids else []
        deliveries = stop_ids.filtered(lambda s: s.stop_type == "delivery") if stop_ids else []

        pickup_city   = self._extract_city(pickups[:1].address if pickups else "")
        delivery_city = self._extract_city(deliveries[:1].address if deliveries else "")

        load_lines = []
        if pickups and deliveries:
            max_len = max(len(pickups), len(deliveries))
            for idx in range(max_len):
                pu = pickups[idx] if idx < len(pickups) else False
                de = deliveries[idx] if idx < len(deliveries) else False
                if pu or de:
                    load_lines.append(
                        f"Load {idx + 1}: "
                        f"{(pu.address if pu else 'N/A')} -> {(de.address if de else 'N/A')}"
                    )

        load_summary = "<br/>".join(load_lines) if load_lines else "Route details to be confirmed."

        n_stops        = len(stop_ids) if stop_ids else 0
        total_pallets  = int(getattr(lead, "total_pallets",     0) or 0)
        total_weight   = float(getattr(lead, "total_weight_lbs",  0.0) or 0.0)
        total_distance = float(getattr(lead, "total_distance_km", 0.0) or 0.0)
        total_hours    = float(getattr(lead, "total_drive_hours",  0.0) or 0.0)
        suggested_rate = float(getattr(lead, "suggested_rate",    0.0) or 0.0)

        return (
            f"Hello {lead.partner_name or 'Team'},<br/><br/>"
            "Thank you for the opportunity. Please see our provisional quote below:<br/><br/>"
            f"Route: <b>{pickup_city or 'Pickup TBC'} → {delivery_city or 'Delivery TBC'}</b><br/>"
            + (f"Stops: <b>{n_stops}</b><br/>" if n_stops else "")
            + (f"Total pallets: <b>{total_pallets}</b><br/>" if total_pallets else "")
            + (f"Total weight: <b>{total_weight:,.0f} lbs</b><br/>" if total_weight else "")
            + (f"Distance: <b>{total_distance:.1f} km</b><br/>" if total_distance else "")
            + (f"Estimated drive: <b>{total_hours:.2f} hrs</b><br/>" if total_hours else "")
            + (f"Proposed rate: <b>${suggested_rate:,.0f}</b><br/>" if suggested_rate else "")
            + f"<br/>{load_summary}<br/><br/>"
            "This draft is subject to appointment times, dock/accessorial requirements, and final confirmation. "
            "If approved, please send your PO/rate confirmation and we will secure capacity immediately.<br/><br/>"
            "Best regards,<br/>"
            "PremaFirm Logistics Dispatch"
        )

    def _log_pricing_history_from_wizard(self):
        for wizard in self:
            if wizard.model != "crm.lead":
                continue

            if getattr(wizard, "subtype_is_log", False):
                continue

            try:
                res_ids = wizard._evaluate_res_ids()
            except Exception:
                res_ids = []

            if len(res_ids) != 1:
                continue

            lead = self.env["crm.lead"].browse(res_ids[0]).exists()
            if not lead:
                continue

            body = wizard.body or ""
            price_matches = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", body)
            final_price = float(price_matches[-1].replace(",", "")) if price_matches else 0.0

            stop_ids   = getattr(lead, "dispatch_stop_ids", [])
            pickups    = stop_ids.filtered(lambda s: s.stop_type == "pickup") if stop_ids else []
            deliveries = stop_ids.filtered(lambda s: s.stop_type == "delivery") if stop_ids else []

            pickup_city   = self._extract_city(pickups[:1].address if pickups else "")
            delivery_city = self._extract_city(deliveries[:1].address if deliveries else "")

            try:
                self.env["premafirm.pricing.history"].create({
                    "lead_id":       lead.id,
                    "customer_id":   lead.partner_id.id,
                    "pickup_city":   pickup_city,
                    "delivery_city": delivery_city,
                    "distance_km":   float(getattr(lead, "total_distance_km", 0.0) or 0.0),
                    "pallets":       int(getattr(lead, "total_pallets", 0) or 0),
                    "weight":        float(getattr(lead, "total_weight_lbs", 0.0) or 0.0),
                    "final_price":   final_price,
                })
            except Exception:
                pass  # Don't block mail sending if history logging fails

    def action_send_mail(self):
        self._log_pricing_history_from_wizard()
        return super().action_send_mail()
