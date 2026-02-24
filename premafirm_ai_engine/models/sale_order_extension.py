import base64

from odoo import api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")

    pickup_city = fields.Char()
    delivery_city = fields.Char()

    total_pallets = fields.Integer()
    total_weight_lbs = fields.Float()
    total_distance_km = fields.Float()

    load_reference = fields.Char()
    load_ids = fields.One2many("premafirm.load", "sale_order_id", string="Loads")

    def action_generate_pod(self):
        self.ensure_one()
        if len(self.load_ids) == 1:
            return self.load_ids.action_generate_pod()
        return {
            "type": "ir.actions.act_window",
            "name": "Loads",
            "res_model": "premafirm.load",
            "view_mode": "list,form",
            "domain": [("sale_order_id", "=", self.id)],
            "context": {"default_sale_order_id": self.id},
        }

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        for order in orders:
            if not order.load_ids:
                order.load_ids = [
                    (
                        0,
                        0,
                        {
                            "vehicle_id": order.opportunity_id.assigned_vehicle_id.id,
                            "route_reference": order.load_reference,
                            "bol_number": order.premafirm_bol,
                        },
                    )
                ]
        return orders

    def action_confirm(self):
        result = super().action_confirm()
        for order in self:
            if order.opportunity_id:
                order.opportunity_id.ai_locked = True
            for load in order.load_ids:
                if not load.vehicle_id or not load.driver_id:
                    continue
                report_action = self.env.ref("premafirm_ai_engine.action_report_premafirm_load_pod")
                pdf_content, _ = report_action._render_qweb_pdf(load.id)
                self.env["ir.attachment"].create(
                    {
                        "name": f"POD-{order.name}-{load.name}.pdf",
                        "datas": base64.b64encode(pdf_content),
                        "res_model": "premafirm.load",
                        "res_id": load.id,
                        "mimetype": "application/pdf",
                    }
                )
        return result


    def _validate_pod_before_invoice(self):
        self.ensure_one()
        stops = self.opportunity_id.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")
        for stop in stops:
            if stop.delivery_status != "delivered":
                raise UserError("Invoice blocked: all delivery stops must be delivered.")
            if not stop.receiver_signature and not stop.no_signature_approved:
                raise UserError("Invoice blocked: delivery signature missing and not approved.")

    def _create_invoices(self, grouped=False, final=False, date=None):
        for order in self:
            order._validate_pod_before_invoice()
        return super()._create_invoices(grouped=grouped, final=final, date=date)

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        vals.update(
            {
                "ref": self.premafirm_po,
                "premafirm_po": self.premafirm_po,
                "premafirm_bol": self.premafirm_bol,
                "premafirm_pod": self.premafirm_pod,
                "load_reference": self.load_reference,
                "payment_reference": self.client_order_ref,
                "invoice_origin": self.name,
            }
        )
        partner_country = self.partner_id.country_id.code
        usa_company = self.env["res.company"].search([("name", "ilike", "usa")], limit=1)
        canada_company = self.env["res.company"].search([("name", "ilike", "can")], limit=1)
        company = usa_company if partner_country == "US" and usa_company else canada_company if canada_company else self.company_id
        vals["company_id"] = company.id

        journal = self.env["account.journal"].search(
            [
                ("type", "=", "sale"),
                ("company_id", "=", company.id),
                ("name", "ilike", "USA" if partner_country == "US" else "CAN"),
            ],
            limit=1,
        )
        if journal:
            vals["journal_id"] = journal.id
        return vals


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    load_id = fields.Many2one("premafirm.load")
    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")])
    stop_address = fields.Char()
    stop_map_url = fields.Char()
    scheduled_time = fields.Datetime(related="scheduled_date", store=True, readonly=False)
    eta_datetime = fields.Datetime()
    stop_distance_km = fields.Float()
    stop_drive_hours = fields.Float()

    def _prepare_invoice_line(self, **optional_values):
        vals = super()._prepare_invoice_line(**optional_values)
        if self.stop_distance_km:
            vals["name"] = f"{self.name or ''} ({self.stop_distance_km:.2f} km)"
        return vals
