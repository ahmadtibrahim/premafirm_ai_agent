import base64
import hashlib

from odoo import models


class PremaDedupeService(models.AbstractModel):
    _name = "prema.dedupe.service"
    _description = "Prema Duplicate Detection Service"

    def hash_attachment(self, attachment):
        binary = base64.b64decode(attachment.datas or b"")
        return hashlib.sha256(binary).hexdigest()

    def find_duplicates(self, attachment):
        digest = self.hash_attachment(attachment)
        docs = self.env["prema.ai.document"].sudo().search([("file_sha256", "=", digest)], limit=20)
        return {"sha256": digest, "duplicate_ids": docs.ids}
