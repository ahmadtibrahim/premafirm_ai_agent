import base64

from odoo import fields, models


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

    def action_generate_pod(self):
        self.ensure_one()
        return self.env.ref("premafirm_ai_engine.action_report_premafirm_pod").report_action(self)

    def action_confirm(self):
        result = super().action_confirm()
        report_action = self.env.ref("premafirm_ai_engine.action_report_premafirm_pod")
        for order in self:
            pdf_content, _ = report_action._render_qweb_pdf(order.id)
            self.env["ir.attachment"].create(
                {
                    "name": f"POD-{order.name}.pdf",
                    "datas": base64.b64encode(pdf_content),
                    "res_model": "sale.order",
                    "res_id": order.id,
                    "mimetype": "application/pdf",
                }
            )
        return result

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

    stop_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")])
    stop_address = fields.Char()
    scheduled_time = fields.Datetime(related="scheduled_date", store=True, readonly=False)
    eta_datetime = fields.Datetime()
    stop_distance_km = fields.Float()
    stop_drive_hours = fields.Float()
