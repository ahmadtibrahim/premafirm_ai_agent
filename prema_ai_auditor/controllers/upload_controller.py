import base64
import json

from odoo import http
from odoo.http import request


class AIUploadController(http.Controller):
    @http.route(
        "/prema_ai/upload_multi",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def upload_multi(self, **kwargs):
        upload_file = kwargs.get("file")
        session_id = int(kwargs.get("session_id"))

        attachment = request.env["ir.attachment"].sudo().create(
            {
                "name": upload_file.filename,
                "datas": base64.b64encode(upload_file.read()),
                "res_model": "prema.ai.session",
                "res_id": session_id,
                "type": "binary",
                "mimetype": upload_file.content_type,
            }
        )

        request.env["prema.ai.document"].sudo().create(
            {
                "session_id": session_id,
                "attachment_id": attachment.id,
            }
        )

        cron = request.env.ref("prema_ai_auditor.ir_cron_prema_ai_document_processing")
        cron.sudo()._trigger()

        payload = {
            "attachment_id": attachment.id,
            "mimetype": attachment.mimetype,
            "name": attachment.name,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json")],
        )
