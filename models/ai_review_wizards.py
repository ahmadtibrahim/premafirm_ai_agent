import json
import logging
from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


class PremafirmNotesRewriteWizard(models.TransientModel):
    _name = "premafirm.notes.rewrite.wizard"
    _description = "AI Notes Rewrite — Review Before Applying"

    estimator_id   = fields.Many2one("premafirm.rate.estimator", required=True, ondelete="cascade")
    current_notes  = fields.Text(string="Current Notes", readonly=True)
    ai_suggestion  = fields.Text(string="AI Suggestion", readonly=True)
    status         = fields.Selection([
        ("pending",   "Pending AI"),
        ("ready",     "Ready to Review"),
        ("error",     "Error"),
    ], default="pending")
    error_message  = fields.Char(readonly=True)

    def action_analyze(self):
        self.ensure_one()
        result = self.estimator_id.ai_rewrite_notes_rpc(self.estimator_id.id)
        if result.get("error"):
            self.write({"status": "error", "error_message": result["error"]})
        else:
            self.write({
                "current_notes": result.get("original", ""),
                "ai_suggestion": result.get("suggestion", ""),
                "status": "ready",
            })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_accept(self):
        self.ensure_one()
        if not self.ai_suggestion:
            raise exceptions.UserError("No AI suggestion to apply.")
        self.estimator_id.write({"notes": self.ai_suggestion})
        return {"type": "ir.actions.act_window_close"}

    def action_reject(self):
        return {"type": "ir.actions.act_window_close"}


class PremafirmStopsReviewWizard(models.TransientModel):
    _name = "premafirm.stops.review.wizard"
    _description = "AI Stops Review — Review Before Applying"

    estimator_id   = fields.Many2one("premafirm.rate.estimator", required=True, ondelete="cascade")
    before_json    = fields.Text(string="Current Stops (JSON)", readonly=True)
    after_json     = fields.Text(string="AI Suggested Stops (JSON)", readonly=True)
    suggestions    = fields.Text(string="AI Suggestions", readonly=True)
    status         = fields.Selection([
        ("pending", "Pending AI"),
        ("ready",   "Ready to Review"),
        ("error",   "Error"),
    ], default="pending")
    error_message  = fields.Char(readonly=True)

    def action_analyze(self):
        self.ensure_one()
        result = self.estimator_id.ai_review_stops_rpc(self.estimator_id.id)
        if result.get("error"):
            self.write({"status": "error", "error_message": result["error"]})
        else:
            suggestions_text = "\n".join(
                f"• [{s.get('action','?').upper()}] Stop #{s.get('stop_index','?')}: {s.get('reason','')}"
                for s in result.get("suggestions", [])
            ) or "No changes suggested."
            self.write({
                "before_json": json.dumps(result.get("before", []), indent=2),
                "after_json":  json.dumps(result.get("after", []), indent=2),
                "suggestions": suggestions_text,
                "status": "ready",
            })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_apply(self):
        self.ensure_one()
        if not self.after_json:
            raise exceptions.UserError("No AI suggestions to apply.")
        try:
            new_stops = json.loads(self.after_json)
        except Exception:
            raise exceptions.UserError("Could not parse AI suggestions. Please try again.")
        self.estimator_id.ai_review_stops_apply_rpc(self.estimator_id.id, new_stops)
        return {"type": "ir.actions.act_window_close"}

    def action_reject(self):
        return {"type": "ir.actions.act_window_close"}
