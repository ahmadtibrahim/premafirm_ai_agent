from pathlib import Path

from odoo import api, fields, models


class PremaSchemaBrowser(models.TransientModel):
    _name = "prema.schema.browser"
    _description = "Prema Schema Browser (Reference Snapshot)"

    model_name = fields.Char(required=True)
    field_lines = fields.Text(readonly=True)

    @api.model
    def _reference_root(self):
        path = self.env["ir.config_parameter"].sudo().get_param("prema_ai.schema.reference_path", default="/reference")
        return Path(path)

    def action_load_fields(self):
        self.ensure_one()
        root = self._reference_root()
        runtime = root / "07_python_model_specs.txt"
        fallback = root / "02_all_fields.sql"

        lines = []
        if runtime.exists():
            for line in runtime.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith(f"{self.model_name}."):
                    lines.append(line)
        if not lines and fallback.exists():
            for line in fallback.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 4 and parts[0] == self.model_name:
                    lines.append(f"{parts[0]}.{parts[1]}: {parts[3]}")

        self.field_lines = "\n".join(lines) or "No fields found for this model in reference snapshot."
        return {
            "type": "ir.actions.act_window",
            "res_model": "prema.schema.browser",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
