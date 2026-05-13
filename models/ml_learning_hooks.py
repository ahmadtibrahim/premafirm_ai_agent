"""
ML Learning Hooks — records every confirmed human action as knowledge.

Called from: estimator approval, invoice job creation, rate confirmation,
POD receipt, bill confirmation, stop edits, note rewrites.

Each hook creates or updates a premafirm.ml.knowledge record so future
AI suggestions improve from real dispatcher decisions.
"""
import json
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Knowledge types used across the system
KT_DISPATCH   = "rate_quote"       # confirmed dispatch jobs / routes
KT_BILL       = "bill_autofill"    # vendor bill patterns
KT_INVOICE    = "invoice_flag"     # customer invoice anomalies
KT_STOP_EDIT  = "load_tender"      # stop corrections / address fixes
KT_RATE_CONF  = "rate_conf_autofill"  # rate confirmation autofill


class PremafirmMLLearningHooks(models.AbstractModel):
    """Mixin — attach to any model to get easy ML save helpers."""
    _name = "premafirm.ml.learning"
    _description = "ML Learning Hooks"

    @api.model
    def ml_save(self, knowledge_type, input_context, good_output,
                correction_note=None, origin="approved", weight=1.5):
        """Upsert an ML knowledge entry.  Returns the knowledge record."""
        KB = self.env["premafirm.ml.knowledge"].sudo()
        # Try to find existing entry by context signature (first 120 chars)
        ctx_key = (input_context or "")[:120]
        existing = KB.search([
            ("knowledge_type", "=", knowledge_type),
            ("input_context", "like", ctx_key),
        ], limit=1)
        if existing:
            existing.write({
                "good_output":    good_output if isinstance(good_output, str) else json.dumps(good_output, default=str),
                "correction_note": correction_note or existing.correction_note,
                "origin":          "edited" if correction_note else existing.origin,
                "weight":          min(existing.weight + 0.3, 5.0),
            })
            return existing
        return KB.create({
            "knowledge_type":  knowledge_type,
            "input_context":   input_context,
            "good_output":     good_output if isinstance(good_output, str) else json.dumps(good_output, default=str),
            "correction_note": correction_note,
            "origin":          origin,
            "weight":          weight,
        })


class PremafirmRateEstimatorML(models.Model):
    """Extend the rate estimator to save ML knowledge when estimates are confirmed."""
    _inherit = "premafirm.rate.estimator"

    def action_dispatch_to_fleetbase(self):
        result = super().action_dispatch_to_fleetbase()
        # Only save on successful dispatch (not duplicate-skip warning)
        if self.fleetbase_order_id:
            self._save_estimate_to_ml()
        return result

    def _save_estimate_to_ml(self):
        """Save confirmed dispatch estimate as ML knowledge."""
        try:
            stops = []
            if self.stop_ids:
                stops = [s.as_dict() for s in self.stop_ids.sorted("sequence")]
            elif self.stops_json:
                try:
                    stops = json.loads(self.stops_json)
                except Exception:
                    pass

            input_ctx = (
                f"Dispatch: {self.origin_address or ''} → {self.destination_address or ''} | "
                f"Truck: {self.vehicle_id.name if self.vehicle_id else ''} | "
                f"Stops: {len(stops)} | "
                f"Date: {self.scheduled_at.strftime('%Y-%m-%d') if self.scheduled_at else ''}"
            )
            good_output = json.dumps({
                "vehicle_id":     self.vehicle_id.id if self.vehicle_id else None,
                "vehicle_name":   self.vehicle_id.name if self.vehicle_id else "",
                "origin":         self.origin_address,
                "destination":    self.destination_address,
                "distance_km":    self.distance_km,
                "duration_hrs":   self.duration_hrs,
                "suggested_rate": self.suggested_rate,
                "total_cost":     self.total_cost,
                "stops":          stops,
                "fleetbase_id":   self.fleetbase_order_id,
            }, default=str)

            self.env["premafirm.ml.learning"].sudo().ml_save(
                knowledge_type=KT_DISPATCH,
                input_context=input_ctx,
                good_output=good_output,
                origin="approved",
                weight=2.0,
            )
        except Exception as e:
            _logger.warning("Could not save estimate to ML: %s", e)

    def write(self, vals):
        # Detect manual stop edits and note rewrites → save corrections to ML
        stop_edit = "stop_ids" in vals
        note_edit = "notes" in vals and vals["notes"] != (self.notes or "")
        result = super().write(vals)
        if stop_edit or note_edit:
            for rec in self.filtered(lambda r: r.fleetbase_order_id):
                try:
                    rec._save_estimate_to_ml_correction(
                        what="stops_edited" if stop_edit else "notes_edited"
                    )
                except Exception:
                    pass
        return result

    def _save_estimate_to_ml_correction(self, what="edited"):
        """Save a correction to ML when dispatcher manually changes something."""
        try:
            self.env["premafirm.ml.learning"].sudo().ml_save(
                knowledge_type=KT_STOP_EDIT,
                input_context=(
                    f"Correction [{what}]: {self.origin_address or ''} → {self.destination_address or ''} | "
                    f"Job: {self.job_day_ref or ''}"
                ),
                good_output=json.dumps({
                    "stops": [s.as_dict() for s in self.stop_ids.sorted("sequence")] if self.stop_ids else [],
                    "notes": self.notes or "",
                    "what": what,
                }, default=str),
                correction_note=f"Dispatcher manually edited: {what}",
                origin="edited",
                weight=2.5,
            )
        except Exception as e:
            _logger.warning("Could not save stop correction to ML: %s", e)
