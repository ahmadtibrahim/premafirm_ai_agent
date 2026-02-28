import base64
import io

from odoo import models


class PremaOCRService(models.AbstractModel):
    _name = "prema.ocr.service"
    _description = "Prema OCR Service"

    def extract_text(self, attachment):
        import pytesseract
        from pdf2image import convert_from_bytes
        from PIL import Image

        payload = base64.b64decode(attachment.datas or b"")
        if attachment.mimetype == "application/pdf":
            return "\n".join(pytesseract.image_to_string(page) for page in convert_from_bytes(payload))
        if (attachment.mimetype or "").startswith("image/"):
            return pytesseract.image_to_string(Image.open(io.BytesIO(payload)))
        return ""
