import re

from odoo import api, models


class MailComposeMessage(models.TransientModel):
    _inherit = "mail.compose.message"

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)

        model = defaults.get("model") or self.env.context.get("default_model")
        res_id = defaults.get("res_id") or self.env.context.get("default_res_id")
        body = defaults.get("body")
        if body or model != "crm.lead" or not res_id:
            return defaults

        lead = self.env["crm.lead"].browse(res_id).exists()
        if not lead:
            return defaults

        defaults["body"] = self._build_professional_draft(lead)
        return defaults

    @api.model
    def _extract_city(self, address):
        if not address:
            return ""
        return (address.split(",", 1)[0] or "").strip()

    @api.model
    def _build_professional_draft(self, lead):
        pickups = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")
        deliveries = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")

        pickup_city = self._extract_city(pickups[:1].address if pickups else "")
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

        return (
            f"Hello {lead.partner_name or 'Team'},<br/><br/>"
            "Thank you for the opportunity. Please see our provisional quote below:<br/><br/>"
            f"Route: <b>{pickup_city or 'Pickup TBC'} → {delivery_city or 'Delivery TBC'}</b><br/>"
            f"Stops: <b>{len(lead.dispatch_stop_ids)}</b><br/>"
            f"Total pallets: <b>{int(lead.total_pallets or 0)}</b><br/>"
            f"Total weight: <b>{(lead.total_weight_lbs or 0.0):,.0f} lbs</b><br/>"
            f"Distance: <b>{(lead.total_distance_km or 0.0):.1f} km</b><br/>"
            f"Estimated drive: <b>{(lead.total_drive_hours or 0.0):.2f} hrs</b><br/>"
            f"Proposed rate: <b>${(lead.suggested_rate or 0.0):,.0f}</b><br/><br/>"
            f"{load_summary}<br/><br/>"
            "This draft is subject to appointment times, dock/accessorial requirements, and final confirmation. "
            "If approved, please send your PO/rate confirmation and we will secure capacity immediately.<br/><br/>"
            "Best regards,<br/>"
            "PremaFirm Logistics Dispatch"
        )

    def _log_pricing_history_from_wizard(self):
        for wizard in self:
            if wizard.model != "crm.lead" or not wizard.res_id:
                continue

            lead = self.env["crm.lead"].browse(wizard.res_id).exists()
            if not lead:
                continue

            body = wizard.body or ""
            price_matches = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", body)
            final_price = float(price_matches[-1].replace(",", "")) if price_matches else float(lead.suggested_rate or 0.0)

            pickups = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")
            deliveries = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")

            pickup_city = self._extract_city(pickups[:1].address if pickups else "")
            delivery_city = self._extract_city(deliveries[:1].address if deliveries else "")

            self.env["premafirm.pricing.history"].create(
                {
                    "lead_id": lead.id,
                    "customer_id": lead.partner_id.id,
                    "pickup_city": pickup_city,
                    "delivery_city": delivery_city,
                    "distance_km": float(lead.total_distance_km or 0.0),
                    "pallets": int(lead.total_pallets or 0),
                    "weight": float(lead.total_weight_lbs or 0.0),
                    "final_price": final_price,
                }
            )

    def action_send_mail(self):
        # Capture final human-adjusted quote before send; no auto-send logic is introduced.
        self._log_pricing_history_from_wizard()
        return super().action_send_mail()
