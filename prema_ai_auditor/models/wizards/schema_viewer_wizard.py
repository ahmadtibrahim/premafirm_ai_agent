import json

from odoo import fields, models


class PremaSchemaViewerWizard(models.TransientModel):
    _name = "prema.schema.viewer.wizard"
    _description = "Prema Schema Viewer Wizard"

    model_name = fields.Char(required=True)
    runtime_schema = fields.Text(readonly=True)
    reference_schema = fields.Text(readonly=True)
    diff_report = fields.Text(readonly=True)

    def action_load_schema(self):
        self.ensure_one()
        runtime = []
        model = self.env.get(self.model_name)
        if model:
            for fname, field in sorted(model._fields.items()):
                runtime.append(
                    {
                        "name": fname,
                        "type": field.type,
                        "required": bool(field.required),
                        "readonly": bool(field.readonly),
                        "store": bool(getattr(field, "store", False)),
                        "relation": getattr(field, "comodel_name", "") or "",
                    }
                )

        ref_fields = self.env["prema.mapping.validator"]._reference_fields_for_model(self.model_name)
        runtime_names = {f["name"] for f in runtime}
        ref_names = {f["field"] for f in ref_fields}
        missing_runtime = sorted(ref_names - runtime_names)
        missing_reference = sorted(runtime_names - ref_names)

        self.runtime_schema = json.dumps(runtime, indent=2)
        self.reference_schema = json.dumps(ref_fields, indent=2)
        self.diff_report = (
            f"Missing in runtime: {missing_runtime}\n"
            f"Missing in reference: {missing_reference}"
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }
