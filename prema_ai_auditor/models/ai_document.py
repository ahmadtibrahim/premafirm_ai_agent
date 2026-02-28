from odoo import fields, models


class AIDocument(models.Model):
    _name = "prema.ai.document"
    _description = "Prema AI Document"

    session_id = fields.Many2one("prema.ai.session", required=True, ondelete="cascade", index=True)
    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade")
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("processed", "Processed"),
            ("error", "Error"),
            ("approved", "Approved"),
            ("draft_created", "Draft Created"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    classification = fields.Selection(
        [
            ("vendor_bill", "Vendor Bill"),
            ("receipt", "Receipt"),
            ("bank_statement", "Bank Statement"),
            ("other", "Other"),
        ],
        default="other",
        required=True,
    )
    extracted_text = fields.Text()
    advice = fields.Text()
    vendor_name = fields.Char()
    invoice_number = fields.Char()
    invoice_date = fields.Date()
    amount_total = fields.Float()
    flag_duplicate = fields.Boolean(default=False, index=True)
    move_id = fields.Many2one("account.move", readonly=True, copy=False)
