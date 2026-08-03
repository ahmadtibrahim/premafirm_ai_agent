import json
import logging
from datetime import datetime as _dt
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


class PremafirmStopsReviewWizardStop(models.TransientModel):
    _name = "premafirm.stops.review.wizard.stop"
    _description = "Stops Review Wizard — Stop Line"
    _order = "sequence"

    wizard_id      = fields.Many2one("premafirm.stops.review.wizard", ondelete="cascade")
    is_before      = fields.Boolean(default=False,
                                    help="True = original stop; False = AI-suggested stop")
    sequence       = fields.Integer(default=10)
    stop_type      = fields.Selection([
        ("origin",   "Origin"),
        ("pickup",   "Pickup"),
        ("delivery", "Delivery"),
        ("return",   "Return"),
    ], default="pickup", required=True, string="Type")
    is_system      = fields.Boolean(default=False)
    name           = fields.Char(string="Company Name")
    address        = fields.Char(string="Address")
    lat            = fields.Float(digits=(10, 6))
    lng            = fields.Float(digits=(10, 6))
    scheduled_time = fields.Datetime(string="Scheduled Time")
    pallets        = fields.Integer()
    weight_lbs     = fields.Float(string="Weight (lbs)", digits=(10, 1))
    liftgate       = fields.Boolean()
    notes          = fields.Char(string="Instructions")

    def as_dict(self):
        return {
            "type":           self.stop_type,
            "stop_type":      self.stop_type,
            "is_system":      self.is_system,
            "name":           self.name or "",
            "address":        self.address or "",
            "lat":            self.lat,
            "lng":            self.lng,
            "scheduled_time": self.scheduled_time.isoformat() if self.scheduled_time else None,
            "notes":          self.notes or "",
            "pallets":        self.pallets or 0,
            "weight_lbs":     self.weight_lbs or 0.0,
            "liftgate":       self.liftgate,
        }


def _parse_stop_datetime(raw):
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.strptime(str(raw), fmt)
        except Exception:
            continue
    return None


def _create_wizard_stops(Stop, stop_list, wizard_id, is_before):
    for i, s in enumerate(stop_list):
        Stop.create({
            "wizard_id":      wizard_id,
            "is_before":      is_before,
            "sequence":       (i + 1) * 10,
            "stop_type":      s.get("type") or s.get("stop_type") or "pickup",
            "is_system":      bool(s.get("is_system")),
            "name":           s.get("name") or s.get("company_name") or "",
            "address":        s.get("address") or "",
            "lat":            float(s.get("lat") or 0),
            "lng":            float(s.get("lng") or 0),
            "scheduled_time": _parse_stop_datetime(s.get("scheduled_time")),
            "pallets":        int(s.get("pallets") or 0),
            "weight_lbs":     float(s.get("weight_lbs") or 0),
            "liftgate":       bool(s.get("liftgate")),
            "notes":          s.get("notes") or "",
        })


class PremafirmStopsReviewWizard(models.TransientModel):
    _name = "premafirm.stops.review.wizard"
    _description = "AI Stops Review — Review Before Applying"

    estimator_id   = fields.Many2one("premafirm.rate.estimator", required=True, ondelete="cascade")
    before_stop_ids = fields.One2many(
        "premafirm.stops.review.wizard.stop", "wizard_id",
        domain=[("is_before", "=", True)],
        string="Current Stops",
    )
    after_stop_ids  = fields.One2many(
        "premafirm.stops.review.wizard.stop", "wizard_id",
        domain=[("is_before", "=", False)],
        string="AI Suggested Stops",
    )
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
                f"  [{s.get('action', '?').upper()}] Stop #{s.get('stop_index', '?')}: {s.get('reason', '')}"
                for s in result.get("suggestions", [])
            ) or "No changes suggested."
            self.write({"suggestions": suggestions_text, "status": "ready"})
            Stop = self.env["premafirm.stops.review.wizard.stop"]
            self.before_stop_ids.unlink()
            self.after_stop_ids.unlink()
            _create_wizard_stops(Stop, result.get("before", []), self.id, is_before=True)
            _create_wizard_stops(Stop, result.get("after", []), self.id, is_before=False)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_apply(self):
        self.ensure_one()
        if not self.after_stop_ids:
            raise exceptions.UserError("No AI suggestions to apply.")
        new_stops = [s.as_dict() for s in self.after_stop_ids.sorted("sequence")]
        self.estimator_id.ai_review_stops_apply_rpc(self.estimator_id.id, new_stops)
        return {"type": "ir.actions.act_window_close"}

    def action_reject(self):
        return {"type": "ir.actions.act_window_close"}
