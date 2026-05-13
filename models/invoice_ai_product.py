from odoo import fields, models


class PremaFirmInvoiceAIProduct(models.Model):
    _name = "premafirm.invoice.ai.product"
    _description = "AI-Selectable Invoice Products"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    product_id = fields.Many2one("product.product", required=True, string="Product")
    service_type = fields.Selection(
        [
            ("ltl", "LTL Freight"),
            ("ftl", "FTL Freight"),
            ("local", "Local Delivery"),
            ("other", "Other"),
        ],
        string="Service Type",
        required=True,
    )
    ai_enabled = fields.Boolean(string="AI Can Select", default=True)
    description_hint = fields.Text(
        string="Description Hint",
        help="Optional formatting hint for AI when generating the description for this service type.",
    )
