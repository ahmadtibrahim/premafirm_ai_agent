"""MP2 text-first workflow — structured stops on the estimator scenario
request.

One row per operational stop the estimator will route/price with. Rows are
populated by text extraction and editable from the panel; once a row is
reviewed (user touched it) the extraction must NOT silently overwrite it —
the estimate response flags the detected change and the user accepts or
rejects it.

These rows are the authoritative reviewed source for routing, distance,
capacity, pricing, quotation creation and booking: `_build_payload` reads
them (not the raw extraction) whenever they exist.
"""
from odoo import api, fields, models


class EstimatorStructuredStop(models.Model):
    _name = "premafirm.estimator.structured.stop"
    _description = "Estimator structured stop (MP2 review control)"
    _order = "request_id, sequence"

    request_id = fields.Many2one(
        "premafirm.estimator.scenario.request", required=True,
        ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)

    stop_type = fields.Selection([
        ("pickup", "Pickup"),
        ("delivery", "Delivery"),
    ], required=True, default="delivery")
    source = fields.Selection([
        ("extracted", "Extracted from text"),
        ("manual", "Manually added"),
    ], default="extracted")
    reviewed = fields.Boolean(
        default=False,
        help="User reviewed/edited this row — extraction may not silently "
             "replace it.")

    # ── Location identity ────────────────────────────────────────────
    saved_location_id = fields.Many2one(
        "prema.dispatch.location", string="Saved Location",
        ondelete="set null")
    company_name = fields.Char()
    address = fields.Char()
    city = fields.Char()
    province = fields.Char()
    postal_code = fields.Char()
    place_id = fields.Char()
    lat = fields.Float(digits=(10, 6))
    lng = fields.Float(digits=(10, 6))

    # ── Quantity (per-stop, preserved individually) ──────────────────
    pallets = fields.Integer()
    cases = fields.Integer()
    weight_lbs = fields.Float(digits=(10, 1))

    # ── Timing ───────────────────────────────────────────────────────
    stop_date = fields.Date(string="Requested date")
    time_window_type = fields.Selection([
        ("any", "Anytime"),
        ("exact", "Exact time"),
        ("window", "Time window"),
    ], default="any")
    exact_time = fields.Float(string="Exact time (24h)")
    window_start = fields.Float(string="Window start (24h)")
    window_end = fields.Float(string="Window end (24h)")

    instructions = fields.Text()

    # ── Facility scheduling settings (frozen from the Saved Location,
    #    same authority Prema Dispatch schedules with) ─────────────────
    operating_hours_snapshot = fields.Json()
    tz_name = fields.Char(default="America/Toronto")
    service_time_minutes = fields.Integer(
        help="Loading/unloading duration from the Saved Location's "
             "planning settings (per stop type).")
    per_pallet_service_minutes = fields.Integer()

    # ── Location status (computed from the resolution above) ─────────
    status = fields.Selection([
        ("saved_reused", "Reused Saved Location"),
        ("manual", "Manual Address"),
        ("new_pending", "New Location — Pending Review"),
        ("incomplete", "Incomplete Address — Not Saved"),
    ], compute="_compute_status", store=True)

    # NOTE: this @depends deliberately does NOT chain through
    # saved_location_id.verification_state.  verification_state lives on
    # prema.dispatch.location — a model in prema_dispatch, which depends on
    # this module and therefore loads one depth AFTER the engine in every
    # module graph.  A path-depends across that boundary would make every
    # engine schema upgrade crash: init_models runs the first
    # mark_modified of the run, which resolves the full registry trigger
    # map while prema.dispatch.location is still unregistered ("dependency
    # field ... not found in model _unknown").  The dispatch side pokes
    # these rows (modified(['saved_location_id'])) when a linked location's
    # verification_state changes, which recomputes status then.
    @api.depends("saved_location_id", "address", "city", "province",
                 "postal_code")
    def _compute_status(self):
        for rec in self:
            loc = rec.saved_location_id
            if loc:
                # A location still awaiting its review is shown as the
                # "New Location — Pending Review" the estimator created;
                # verified/legacy locations are genuine reuses.
                if loc.verification_state == "pending_review":
                    rec.status = "new_pending"
                else:
                    rec.status = "saved_reused"
            elif rec.address and (rec.postal_code or
                                  (rec.city and rec.province)):
                rec.status = "manual"
            elif rec.address:
                rec.status = "incomplete"
            else:
                rec.status = "incomplete"

    def _stop_dict(self):
        """Panel-ready dict (matches the JSON contract the panel renders)."""
        self.ensure_one()
        loc = self.saved_location_id
        return {
            "id": self.id,
            "sequence": self.sequence,
            "stop_type": self.stop_type,
            "source": self.source,
            "reviewed": self.reviewed,
            "status": self.status or "incomplete",
            "saved_location_id": loc.id if loc else False,
            "saved_location_name": loc.name if loc else "",
            "company_name": self.company_name or "",
            "address": self.address or "",
            "city": self.city or "",
            "province": self.province or "",
            "postal_code": self.postal_code or "",
            "pallets": self.pallets or 0,
            "cases": self.cases or 0,
            "weight_lbs": self.weight_lbs or 0.0,
            "stop_date": (self.stop_date.isoformat()
                          if self.stop_date else False),
            "time_window_type": self.time_window_type or "any",
            "exact_time": self.exact_time or 0.0,
            "window_start": self.window_start or 0.0,
            "window_end": self.window_end or 0.0,
            "instructions": self.instructions or "",
            "service_time_minutes": self.service_time_minutes or 0,
            "per_pallet_service_minutes":
                self.per_pallet_service_minutes or 0,
            "operating_hours_snapshot":
                self.operating_hours_snapshot or {},
        }
