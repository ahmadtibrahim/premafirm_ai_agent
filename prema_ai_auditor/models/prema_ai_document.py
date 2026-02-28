import hashlib

from odoo import api, fields, models


class PremaAIDocument(models.Model):
    _inherit = "prema.ai.document"

    batch_name = fields.Char(default="Upload Batch")
    mode = fields.Selection(
        [("advice_only", "Advice Only"), ("bill_attach", "Attach to Bill"), ("invoice_attach", "Attach to Invoice")],
        default="advice_only",
        required=True,
    )
    state = fields.Selection(
        [("uploaded", "Uploaded"), ("processing", "Processing"), ("ready", "Ready"), ("proposed", "Proposed"), ("done", "Done")],
        default="uploaded",
        required=True,
    )
    attachment_ids = fields.Many2many("ir.attachment", string="Batch Attachments")
    file_sha256 = fields.Char(index=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.attachment_id and not record.file_sha256:
                record.file_sha256 = hashlib.sha256((record.attachment_id.datas or "").encode() if isinstance(record.attachment_id.datas, str) else (record.attachment_id.datas or b"")).hexdigest()
            if record.attachment_id and not record.attachment_ids:
                record.attachment_ids = [(4, record.attachment_id.id)]
        return records
