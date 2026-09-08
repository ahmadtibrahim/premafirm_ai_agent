"""Estimator scenario request (master §9-13) — the auditable entry point
that turns ONE customer message into ONE consolidated estimator response.

Flow:
  1. Stop extraction reuses the estimator's own canonical parsers
     (``_extract_stops_from_text`` / ``extract_stops_from_file_rpc``) and
     the Dispatch document parser — never a second extraction dialect.
  2. Itinerary + reposition/return legs come from MapboxService with the
     truck's height/GVWR constraints (the estimator's routing authority).
  3. Dispatch-side authority is invoked ONLY through the guarded
     ``logistics.estimator.bridge`` env entry: three scenario cards, truck
     availability/ELD, customer pricing intel, load pairing and route
     development.  When that entry is absent the request still completes
     with an honest "dispatch integration offline" message.
  4. The request record persists inputs_json/response_json for audit
     (§12.3) — nothing else is created, and nothing here ever sends mail,
     confirms a quote, creates a booking, allocates a truck or mutates a
     plan.  Conversion actions are separate, explicit, human clicks.

Authority boundaries: Prema AI extracts/drafts/advises; Prema Dispatch
prices, costs, schedules and owns capacity.  This module never calls a
Dispatch model directly — only the bridge above, behind a registry guard.
"""

import datetime
import json
import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_MODULE_NAME = "premafirm_ai_engine"
_FSA_RE = re.compile(r"\b([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)\b")
# Bare forward-sortation-area fallback (e.g. "M5V" when the message's
# postal was truncated to its first three characters by the extractor).
_FSA_BARE_RE = re.compile(r"\b([A-Za-z]\d[A-Za-z])\b")
_US_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


class EstimatorScenarioRequest(models.Model):
    _name = "premafirm.estimator.scenario.request"
    _description = "Estimator scenario request (audit snapshot)"
    _order = "id desc"

    name = fields.Char(string="Reference", readonly=True, copy=False,
                       default=lambda self: self._default_name())
    partner_id = fields.Many2one("res.partner", string="Customer",
                                 ondelete="set null")
    lead_id = fields.Many2one("crm.lead", string="Opportunity",
                              ondelete="set null")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck",
                                 required=True)
    state = fields.Selection([
        ("draft", "Draft"),
        ("computed", "Computed"),
        ("error", "Error"),
    ], default="draft", string="State")
    inputs_json = fields.Json(string="Inputs", readonly=True)
    response_json = fields.Json(string="Response", readonly=True)
    user_id = fields.Many2one(
        "res.users", string="User", default=lambda self: self.env.user,
        readonly=True)
    engine_version = fields.Char(string="Engine version", readonly=True)
    suggested_sell = fields.Float(string="Suggested sell (card 1)", digits=0)
    margin_pct = fields.Float(
        string="Margin — markup on cost (%)",
        help="Markup-on-cost percentage the scenarios were priced with. "
             "Empty means the server default applied.")
    distance_km = fields.Float(string="Distance (km)", digits=0)
    dispatch_online = fields.Boolean(string="Dispatch bridge online",
                                     readonly=True)
    message = fields.Text(string="Message")

    structured_stop_ids = fields.One2many(
        "premafirm.estimator.structured.stop", "request_id",
        string="Structured stops")
    equipment = fields.Char(string="Extracted equipment")
    instructions = fields.Text(string="Shipment instructions")
    total_pallets = fields.Integer(string="Total pallets")
    total_cases = fields.Integer(string="Total cases")
    total_weight_lbs = fields.Float(string="Total weight (lbs)", digits=0)
    selected_scenario = fields.Char(
        string="Selected scenario",
        help="Scenario key the user selected before creating the draft "
             "Rate Confirmation.")
    quote_id = fields.Many2one(
        "logistics.custom.quote", string="Draft Rate Confirmation",
        ondelete="set null",
        help="The ONE draft quotation created from this estimate "
             "(canonical quote → approval → booking workflow).")

    # ── Entry point (called by the reworked estimator panel) ─────────
    # NOTE: the panel calls these with orm.call(model, name, [], kwargs) —
    # @api.model is what keeps the framework from mis-reading [] as the
    # ids positional argument ("list index out of range" in call_kw).

    @api.model
    def estimate_scenarios_rpc(self, vehicle_id, text_message="", files=None,
                               partner_id=0, avoid_tolls=True,
                               allow_cross_border=False, scheduled_at=None,
                               return_to_home=True, margin_pct=None,
                               stops_input=None, request_id=None,
                               apply_changes=False,
                               manual_distance_km=None,
                               manual_duration_hrs=None):
        """§1 optional manual override — when geocoding leaves fewer than
        two routable stops the user can supply the lane's road distance
        (km) and optionally the drive time (h) instead. Only honored for
        a plain two-stop lane; the card is labeled as a manual override."""
        """One message → one consolidated response (scenarios + itinerary
        + intel + pairing + structured stops).  Creates ONLY audit/
        review records — never a quote, booking, invoice or communication.

        MP2: `stops_input` carries the panel's structured stop rows (the
        reviewed authority); extraction populates them and never silently
        overwrites a reviewed row — the response's `stop_conflicts` asks
        the user to accept or reject detected text changes.

        Always returns a STRUCTURED dict (never a bare exception)::
            success             bool — estimate completed
            validation_errors   [str] — input problems the user must fix
            operational_warnings [str] — degradations (unroutable stop,
                                         city-level geocode, …)
            stops               [{seq, kind, name, address, fsa, qty}]
            structured_stops    [panel-ready stop dicts]
            stop_conflicts      [{sequence, current, proposed}]
            extraction          {equipment, requested_pickup_date,
                                 requested_pickup_time, instructions,
                                 total_pallets, total_cases,
                                 total_weight_lbs}
            route               {distance_km, duration_hrs, itinerary, …}
            scenarios / intel / pairing / lead_id — dispatch answer
            message             user-facing headline (success or failure)
            error_detail        technical detail (support only)
        """
        if manual_distance_km:
            try:
                manual_distance_km = float(manual_distance_km)
                if manual_distance_km <= 0:
                    manual_distance_km = False
            except (TypeError, ValueError):
                manual_distance_km = False
        else:
            manual_distance_km = False
        if manual_duration_hrs:
            try:
                manual_duration_hrs = float(manual_duration_hrs)
                if manual_duration_hrs <= 0:
                    manual_duration_hrs = False
            except (TypeError, ValueError):
                manual_duration_hrs = False
        else:
            manual_duration_hrs = False
        request = self.env["premafirm.estimator.scenario.request"]
        try:
            vehicle = self.env["fleet.vehicle"].sudo().browse(int(vehicle_id))
            if not vehicle.exists():
                return self._rpc_failure(
                    request,
                    message="Truck %s was not found — pick a truck from the "
                            "list." % vehicle_id)
            try:
                margin_val = float(margin_pct)
            except (TypeError, ValueError):
                margin_val = False

            # Reuse the request when the panel re-estimates the same
            # session (stop edits, margin apply) so the structured stops
            # stay ONE reviewed record instead of fragmenting per click.
            if request_id:
                request = self.sudo().browse(int(request_id))
            if not (request and request.exists()):
                request = self.sudo().create({
                    "vehicle_id": vehicle.id,
                    "partner_id": int(partner_id or 0) or False,
                    "engine_version": self._engine_version(),
                    "state": "draft",
                    "margin_pct": margin_val,
                })
            elif margin_val is not False:
                request.margin_pct = margin_val
            elif not request.margin_pct:
                request.margin_pct = False

            # ── Extraction (text facts + file stops) ─────────────────
            warnings = []
            facts = {}
            all_stops = []
            estimator = self.env["premafirm.rate.estimator"].sudo().browse()
            if text_message and text_message.strip():
                try:
                    # §7 loadboard hygiene: paste clutter (lone "svg"
                    # tokens, symbol-only lines, repeated fragments) is
                    # dropped BEFORE extraction so facts are never parsed
                    # twice or mangled by formatting noise.
                    text_message = self._sanitize_posting_text(text_message)
                    facts = estimator._extract_work_order_text(
                        text_message) if hasattr(
                        estimator, "_extract_work_order_text") else {}
                    # Sanitizer: a pickup that mirrors the first
                    # delivery's quantity while the deliveries carry the
                    # split total is an extractor copy artifact — the
                    # pickup's own quantity stays 0.
                    fst = list(facts.get("stops") or [])
                    picks = [s for s in fst
                             if str(s.get("type") or "").lower()
                             in ("pickup", "origin")]
                    drops = [s for s in fst
                             if str(s.get("type") or "").lower()
                             in ("delivery", "dropoff")]
                    drop_sum = sum(int(s.get("pallets") or 0)
                                   for s in drops)
                    if picks and drops and drop_sum:
                        first_drop = int(drops[0].get("pallets") or 0)
                        for s in picks:
                            if int(s.get("pallets") or 0) == first_drop \
                                    and drop_sum > first_drop:
                                s["pallets"] = 0
                                s["cases"] = 0
                                s["weight_lbs"] = 0
                    for s in fst:
                        s = dict(s)
                        s["_source"] = "text"
                        all_stops.append(s)
                except Exception as exc:
                    warnings.append("Text extraction failed (%s) — falling "
                                    "back to the classic parser."
                                    % str(exc)[:120])
                    facts = {}
            if files:
                f_stops, f_warnings = self._collect_stops(
                    request, "", files or [], vehicle)
                all_stops.extend(f_stops or [])
                warnings.extend(f_warnings or [])
            if not all_stops and not (stops_input or []):
                message = ("Could not detect any stops from the provided "
                           "input. Include where the freight is picked up "
                           "and where it goes — city or address, postal "
                           "code or FSA (e.g. \"pick up 12 pallets in "
                           "Toronto M5V, deliver Montreal H4N\").")
                request.sudo().write({
                    "state": "error", "message": message})
                return self._rpc_failure(
                    request, message=message,
                    errors=[message],
                    detail="No stops extracted — no pickup or delivery "
                           "recognized in text/file input.")

            # ── Structured stops: merge panel rows + extraction ───────
            rows, conflicts = self._merge_structured_stops(
                request, stops_input or [], all_stops,
                apply_changes=bool(apply_changes))
            # Geocode rows without coordinates so routing stays possible
            # for manual/legacy-shaped addresses — INCLUDING loadboard
            # postings whose stop carries no street (postal-code or
            # city-only). Ordered fallback per row (§1): full address →
            # postal code w/ city/province → city/province centre. Street
            # text is never invented; approximate resolutions are labeled
            # and reported as warnings, and rows are never saved as
            # verified addresses by _match_stop_locations (street-less
            # rows are only ever coords + labels, no location record).
            from ..services.mapbox_service import MapboxService
            mbx = MapboxService(self.env)
            postal_filled = []
            unresolved = []
            for rec in rows:
                if (rec.lat and rec.lng) and rec.postal_code:
                    continue
                address = (rec.address or "").strip()
                city = (rec.city or "").strip()
                province = (rec.province or "").strip()
                postal = (rec.postal_code or "").strip()
                label = " ".join(filter(None, (city, province)))
                if not (address or city or province or postal):
                    unresolved.append(label or rec.company_name or
                                      "a stop")
                    continue
                # Ordered resolution attempts — first hit wins.
                attempts = []
                if address:
                    attempts.append(("street", " ".join(filter(None, (
                        address, city, province, postal)))))
                if postal:
                    attempts.append(("postal", " ".join(filter(None, (
                        city, province, postal)))))
                if city and province:
                    attempts.append(("city", "%s, %s, Canada"
                                     % (city, province)))
                tier_hit = False
                for tier, query in attempts:
                    try:
                        hits = mbx.geocode_address(query)
                    except Exception:
                        hits = []
                    if not hits:
                        continue
                    hit = next((h for h in hits
                                if "Canada" in h.get("place_name", "")),
                               None) or hits[0]
                    vals = {}
                    if not (rec.lat and rec.lng):
                        vals.update({
                            "lat": float(hit["lat"]),
                            "lng": float(hit["lng"])})
                    if not postal:
                        hit_postal = (hit.get("postal_code")
                                      or hit.get("postcode") or "")
                        if hit_postal:
                            vals["postal_code"] = str(hit_postal)
                            postal_filled.append(rec.id)
                    if vals:
                        rec.sudo().write(vals)
                    tier_hit = tier
                    break
                if not tier_hit:
                    unresolved.append(label or rec.company_name or
                                      "a stop")
                    continue
                if tier_hit == "postal":
                    warnings.append(
                        "%s — no street address was published on the "
                        "posting; routed to the %s postal area "
                        "(approximate — road distance may differ from the "
                        "actual dock)." % (label, postal.upper() or
                                           str(rec.postal_code or "").upper()))
                elif tier_hit == "city":
                    warnings.append(
                        "%s — only the city was published on the posting; "
                        "routed from the city centre (approximate)."
                        % label)
            if unresolved:
                warnings.append(
                    "Could not locate: %s. Routing for that stop is "
                    "unavailable until its address is corrected in the "
                    "stops editor (re-run to resolve) — estimates that "
                    "need it show \"Estimate unavailable\", never a "
                    "fabricated distance." % "; ".join(
                        str(u) for u in dict.fromkeys(unresolved)))
            if postal_filled:
                # The postal changed the match identity — re-resolve
                # those rows (dedupe-safe, never creates duplicates).
                self._match_stop_locations(
                    request.structured_stop_ids.filtered(
                        lambda r: r.id in postal_filled))
            # The customer's requested pickup time ("@8am") binds the
            # first pickup's exact appointment — only when the user has
            # not already set a time on that stop.
            req_time = facts.get("requested_pickup_time")
            if req_time and str(req_time).strip():
                try:
                    hh, mm = str(req_time).strip().split(":")[:2]
                    t_float = float(int(hh)) + float(int(mm)) / 60.0
                    first_pickup = next(
                        (r for r in rows.sorted("sequence")
                         if r.stop_type == "pickup"), None)
                    if first_pickup and not first_pickup.reviewed and \
                            (not first_pickup.exact_time and
                             first_pickup.time_window_type == "any"):
                        first_pickup.sudo().write({
                            "time_window_type": "exact",
                            "exact_time": t_float})
                except Exception:
                    pass
            rows = request.structured_stop_ids.sorted("sequence")
            stop_dicts = [r._stop_dict() for r in rows]
            payload_stops = self._payload_stop_dicts(rows, warnings)

            if not payload_stops:
                message = ("The stops could not be resolved — check the "
                           "addresses above and try again.")
                request.sudo().write({
                    "state": "error", "message": message})
                return self._rpc_failure(
                    request, message=message, errors=[message],
                    detail="No structured stops after merge.")
            self._validate_stop_mix(payload_stops, request)

            payload = self._build_payload(
                request, vehicle, payload_stops, warnings, avoid_tolls,
                allow_cross_border, scheduled_at, return_to_home,
                request_text=text_message, margin_pct=margin_val,
                manual_distance_km=manual_distance_km,
                manual_duration_hrs=manual_duration_hrs)
            self._enrich_payload(request, payload, rows, facts, warnings)
            request.inputs_json = payload

            bridge_ok = "logistics.estimator.bridge" in self.env.registry
            response = {
                "ok": False, "request_id": request.id, "state": "error",
                "dispatch_online": bridge_ok, "route": payload["route"],
                "warnings": warnings, "scenarios": [], "intel": {},
                "pairing": {}, "lead_id": False,
                "success": False, "validation_errors": [],
                "operational_warnings": list(warnings),
                "message": "", "error_detail": False,
                "structured_stops": stop_dicts,
                "stop_conflicts": conflicts,
                "extraction": {
                    "equipment": payload.get("equipment") or "",
                    "requested_pickup_date": payload.get("pickup_date")
                    or False,
                    "requested_delivery_date": payload.get(
                        "requested_delivery_date") or False,
                    "requested_pickup_time": facts.get(
                        "requested_pickup_time") or False,
                    "reference": payload.get("reference") or False,
                    "posted_equipment": payload.get("posted_equipment")
                    or False,
                    "dimensions": payload.get("dimensions") or False,
                    "appointments_required": payload.get(
                        "appointments_required") or False,
                    "instructions": facts.get("instructions") or "",
                    "total_pallets": payload.get("pallets") or 0,
                    "total_cases": payload.get("total_cases") or 0,
                    "total_weight_lbs": payload.get("weight_lbs") or 0.0,
                },
            }
            response["stops"] = [{
                "seq": i + 1,
                "kind": s.get("type"),
                "name": s.get("company_name") or "",
                "address": s.get("address") or "",
                "fsa": s.get("fsa_code") or "",
                "qty": ("%d pallets" % s.get("pallets") or 0)
                       if (s.get("pallets") or 0)
                       else ("%d lb" % s.get("weight_lbs") or 0
                             if (s.get("weight_lbs") or 0) else ""),
            } for i, s in enumerate(payload.get("stops") or [])]
            if not bridge_ok:
                response["ok"] = True
                response["state"] = "computed"
                response["success"] = True
                request.sudo().write({
                    "state": "computed", "dispatch_online": False,
                    "message": "Dispatch integration is offline on this "
                               "database — scenario cards, availability and "
                               "pricing intelligence are unavailable. The "
                               "itinerary and truck facts above are still "
                               "computed."})
                request.response_json = response
                return response

            bridge = self.env["logistics.estimator.bridge"]
            result = bridge.estimate_request(payload)
            response["ok"] = True
            response["state"] = "computed"
            response["success"] = True
            if result.get("error"):
                response["message"] = (
                    "Dispatch scenario engine could not answer: %s"
                    % result.get("message", "unknown error"))
                warnings.append(response["message"])
            else:
                response.update({
                    "scenarios": result.get("scenarios", []),
                    "intel": result.get("intel", {}),
                    "pairing": result.get("pairing", {}),
                    "fatal": result.get("fatal"),
                    "d0": result.get("d0"),
                    "requested_date": result.get("requested_date"),
                })
                warnings = list(result.get("warnings") or warnings)
                response["warnings"] = warnings
                best = next((s for s in result.get("scenarios", [])
                             if s.get("feasible")), None)
                response["suggested_sell"] = \
                    best.get("suggested_sell") if best else False
            response["operational_warnings"] = list(warnings)

            # §5 — blank stop dates come from the resolved pickup date
            # and the calculated schedule; explicit user dates are never
            # overwritten.
            self._fill_stop_dates(request, payload, result)
            stop_dicts = [r._stop_dict()
                          for r in request.structured_stop_ids
                          .sorted("sequence")]
            response["structured_stops"] = stop_dicts

            lead = self.sudo()._find_open_lead(request.partner_id)
            if lead:
                response["lead_id"] = lead.id
            request.sudo().write({
                "state": "computed", "dispatch_online": True,
                "suggested_sell": response.get("suggested_sell") or 0.0,
                "distance_km": payload["route"].get("distance_km") or 0.0,
                "message": "Computed %d scenario(s)."
                           % len(response.get("scenarios") or []),
                "lead_id": lead.id if lead else False,
            })
            request.response_json = response
            return response
        except Exception as exc:
            # Any extraction/routing/bridge failure must leave a visible
            # ERROR audit record (never a phantom row stuck in "draft") and
            # the caller gets the request_id back to find it.  The UI gets a
            # friendly headline; the technical detail is on the record AND
            # in error_detail (small print) — never the bare exception as
            # the only message.
            _logger.exception("estimate_scenarios_rpc failed")
            return self._rpc_failure(
                request,
                message="The estimate could not be completed — adjust the "
                        "request above and try again%s."
                        % ((" (request %s)" % request.name) if request
                           else " without a truck"),
                detail=str(exc)[:400])

    # ── Structured stops (MP2) ───────────────────────────────────────

    _ADDR_TAIL_RE = re.compile(
        r",\s*([^,]+),\s*([A-Z]{2}|Ontario|Quebec|Québec|"
        r"British Columbia|Alberta|Manitoba|Saskatchewan|Nova Scotia|"
        r"New Brunswick|Newfoundland|Prince Edward Island)"
        r"\s*(?:,?\s*([A-Z]\d[A-Z]\s?\d[A-Z]\d))?\s*(?:,?\s*Canada)?\s*$",
        re.IGNORECASE)

    def _fill_address_parts(self, s):
        """Split a full 'street, City, ON K1A 0B1, Canada' address into
        the structured parts when the extractor gave none separately."""
        s = dict(s)
        addr = str(s.get("address") or "").strip()
        if not addr or (s.get("city") and s.get("province")):
            return s
        m = self._ADDR_TAIL_RE.search(addr)
        if not m:
            return s
        s["city"] = s.get("city") or (m.group(1) or "").strip()
        s["province"] = s.get("province") or (m.group(2) or "").strip()
        s["postal_code"] = s.get("postal_code") or \
            (m.group(3) or "").strip()
        s["address"] = addr[:m.start()].strip().rstrip(",")
        return s

    def _merge_structured_stops(self, request, stops_input, extracted,
                                apply_changes=False):
        """Merge panel rows (reviewed authority) with fresh extraction.

        Returns (recordset, conflicts). Conflicts list {sequence,
        stop_type, current:{...}, proposed:{...}} for reviewed rows the
        text changed — the panel asks the user to accept or reject.
        """
        Stop = self.env["premafirm.estimator.structured.stop"].sudo()
        conflicts = []
        panel_stops = [self._fill_address_parts(s) for s in (stops_input or [])
                       if isinstance(s, dict)]
        extracted = [self._fill_address_parts(s) for s in (extracted or [])
                     if isinstance(s, dict)]

        def _type(v):
            v = str(v or "").lower()
            if v in ("pickup", "origin"):
                return "pickup"
            return "delivery"

        def _hhmm(v):
            """'08:00' / '8:00' / 8.0 → hours as float."""
            if v in (None, False, ""):
                return 0.0
            try:
                if isinstance(v, (int, float)):
                    return float(v)
                v = str(v).strip()
                if ":" in v:
                    h, m = v.split(":")[:2]
                    return float(int(h)) + float(int(m)) / 60.0
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        def _norm_addr(s):
            return " ".join(filter(None, (
                str(s.get("address") or "").lower().strip(),
                str(s.get("city") or "").lower().strip(),
                str(s.get("postal_code") or str(s.get("postal") or ""))
                .lower().replace(" ", ""),
            )))

        def _materially_differs(current, proposed):
            if _type(proposed.get("type")) != current["stop_type"]:
                return True
            if _norm_addr(proposed) and \
                    _norm_addr(proposed) != _norm_addr(current):
                return True
            if int(proposed.get("pallets") or 0) != int(
                    current.get("pallets") or 0):
                return True
            if int(proposed.get("cases") or 0) != int(
                    current.get("cases") or 0):
                return True
            if float(proposed.get("weight_lbs") or 0.0) != float(
                    current.get("weight_lbs") or 0.0):
                return True
            if str(proposed.get("company_name") or "").strip() and \
                    str(proposed.get("company_name") or "").strip() != \
                    str(current.get("company_name") or "").strip():
                return True
            return False

        def _row_vals(s, source, reviewed):
            raw_date = s.get("stop_date") or s.get("date") or False
            stop_date = False
            if raw_date:
                # The extractor may echo "tomorrow" / weekday names in the
                # per-stop date — resolve before the Date field sees it.
                stop_date = self._resolve_relative_date(raw_date) or False
            return {
                "sequence": int(s.get("sequence") or 0),
                "stop_type": _type(s.get("type") or s.get("stop_type")),
                "source": source,
                "reviewed": reviewed,
                "saved_location_id": int(s.get("saved_location_id") or 0)
                or False,
                "company_name": s.get("company_name") or False,
                "address": s.get("address") or False,
                "city": s.get("city") or False,
                "province": s.get("province") or False,
                "postal_code": s.get("postal_code")
                or s.get("postal") or False,
                "pallets": int(s.get("pallets") or 0),
                "cases": int(s.get("cases") or 0),
                "weight_lbs": float(s.get("weight_lbs") or 0.0),
                "stop_date": stop_date,
                "time_window_type": s.get("time_window_type") or "any",
                "exact_time": _hhmm(s.get("exact_time")),
                "window_start": _hhmm(s.get("window_start")),
                "window_end": _hhmm(s.get("window_end")),
                "instructions": s.get("instructions")
                or s.get("stop_notes") or False,
                "place_id": s.get("place_id") or False,
                "lat": float(s.get("lat") or 0) or 0.0,
                "lng": float(s.get("lng") or 0) or 0.0,
            }

        # 1. Panel rows first (their sequence order is the review order).
        seq = [10]
        kept_ids = []
        for s in panel_stops:
            vals = _row_vals(s, "manual"
                            if not s.get("id") and not s.get("source")
                            else s.get("source") or "extracted", True)
            vals["sequence"] = seq[0]
            seq[0] += 10
            if s.get("id"):
                rec = Stop.browse(int(s["id"]))
                if rec.exists():
                    rec.sudo().write(vals)
                    kept_ids.append(rec.id)
                    continue
            rec = Stop.create(dict(vals, request_id=request.id))
            kept_ids.append(rec.id)

        # 2. Extracted stops: update unreviewed rows in position, append
        #    new ones, flag (never silently replace) reviewed rows.
        existing = Stop.search([
            ("request_id", "=", request.id),
        ] + ([("id", "in", kept_ids)] if kept_ids else []),
            order="sequence").sorted("sequence")
        for i, s in enumerate(extracted):
            target = existing[i] if i < len(existing) else Stop
            proposed = _row_vals(s, "extracted", False)
            if target and target.reviewed:
                if _materially_differs(
                        {"stop_type": target.stop_type,
                         "address": target.address,
                         "city": target.city,
                         "postal_code": target.postal_code,
                         "company_name": target.company_name,
                         "pallets": target.pallets,
                         "cases": target.cases,
                         "weight_lbs": target.weight_lbs}, s):
                    if apply_changes:
                        target.sudo().write(dict(
                            proposed, reviewed=False, source="extracted",
                            sequence=target.sequence,
                            saved_location_id=False))
                    else:
                        conflicts.append({
                            "sequence": target.sequence,
                            "stop_type": target.stop_type,
                            "current": target._stop_dict(),
                            "proposed": {
                                "stop_type": proposed["stop_type"],
                                "company_name": proposed["company_name"],
                                "address": proposed["address"],
                                "city": proposed["city"],
                                "postal_code": proposed["postal_code"],
                                "pallets": proposed["pallets"],
                                "cases": proposed["cases"],
                                "weight_lbs": proposed["weight_lbs"],
                            },
                        })
                continue
            if target:
                # Re-extraction may have changed the address — the old
                # auto-matched location must not ride along; step 3
                # re-resolves it.
                target.sudo().write(dict(proposed,
                                         sequence=target.sequence,
                                         saved_location_id=False))
                continue
            Stop.create(dict(proposed, request_id=request.id,
                             sequence=seq[0]))
            seq[0] += 10

        rows = Stop.search([("request_id", "=", request.id)],
                           order="sequence")
        self._match_stop_locations(rows)
        return rows, conflicts

    def _match_stop_locations(self, rows):
        """Saved-location resolution per row (only when the address
        changed or no location is linked yet). Re-entrant — safe to run
        again after a geocode filled the postal code."""
        if "logistics.estimator.bridge" not in self.env.registry:
            return
        bridge = self.env["logistics.estimator.bridge"]
        for rec in rows:
            if rec.saved_location_id and rec.reviewed:
                # User-picked or user-confirmed — never re-match.
                continue
            if rec.saved_location_id and not rec.reviewed:
                # Auto-matched earlier; re-extraction may have changed
                # the address — re-resolve to stay truthful.
                rec.sudo().write({"saved_location_id": False})
            if not (rec.address or "").strip():
                continue
            result = bridge.location_match_or_create(
                rec.company_name or "", rec.address or "",
                rec.city or "", rec.province or "",
                rec.postal_code or "", rec.lat or 0.0,
                rec.lng or 0.0, rec.place_id or "")
            fill = {}
            if result.get("saved_location_id"):
                fill["saved_location_id"] = \
                    int(result["saved_location_id"])
                # Inherit the matched location's missing parts — the
                # street-only pickup gains city/province/postal, so
                # the FSA/corridor card and the completeness status
                # both resolve.
                if result.get("city") and not rec.city:
                    fill["city"] = result["city"]
                if result.get("province_code") and not rec.province:
                    fill["province"] = result["province_code"]
                if result.get("postal_code") and not rec.postal_code:
                    fill["postal_code"] = result["postal_code"]
                # Facility scheduling settings (the SAME authority
                # Prema Dispatch schedules with).
                if result.get("operating_hours_snapshot"):
                    fill["operating_hours_snapshot"] = \
                        result["operating_hours_snapshot"]
                if result.get("tz_name"):
                    fill["tz_name"] = result["tz_name"]
                svc = (result.get("service_time_minutes_pickup")
                       if rec.stop_type == "pickup"
                       else result.get("service_time_minutes_delivery"))
                if svc and not rec.service_time_minutes:
                    fill["service_time_minutes"] = int(svc)
                if result.get("per_pallet_service_minutes"):
                    fill["per_pallet_service_minutes"] = int(
                        result["per_pallet_service_minutes"])
            if fill:
                rec.sudo().write(fill)

    def _payload_stop_dicts(self, rows, warnings):
        """Convert structured rows into the payload stop dict shape the
        payload builder routes/prices with."""
        stop_dicts = []
        for rec in rows.sorted("sequence"):
            postal = (rec.postal_code or "").strip()
            address = " ".join(filter(None, (
                rec.address or "", rec.city or "",
                rec.province or "", postal)))
            fsa = re.sub(r"\s", "", postal).upper()[:3] if postal else ""
            # §1 resolution level — how precisely the stop could be
            # placed. Derived from what the request itself carries (no
            # schema change): a street addresses at the dock, a bare
            # postal maps to the postal area, city-only to the city
            # centre. Dispatch labels the approximation; nothing here is
            # ever persisted as a verified customer address.
            has_street = bool((rec.address or "").strip())
            approx_level = ("street" if has_street else
                            ("postal" if postal else
                             ("city" if (rec.city or "").strip() and
                              (rec.province or "").strip() else "none")))
            stop_dicts.append({
                "type": rec.stop_type,
                "company_name": rec.company_name or "",
                "address": address,
                "lat": rec.lat or 0.0,
                "lng": rec.lng or 0.0,
                "pallets": rec.pallets or 0,
                "cases": rec.cases or 0,
                "weight_lbs": rec.weight_lbs or 0.0,
                "liftgate": False,
                "stop_notes": rec.instructions or "",
                "fsa_code": fsa,
                "stop_date": rec.stop_date or False,
                "time_window_type": rec.time_window_type or "any",
                "exact_time": rec.exact_time or 0.0,
                "window_start": rec.window_start or 0.0,
                "window_end": rec.window_end or 0.0,
                "status": rec.status or "incomplete",
                "approx_level": approx_level,
                "operating_hours_snapshot":
                    rec.operating_hours_snapshot or {},
                "tz_name": rec.tz_name or "America/Toronto",
                "service_time_minutes": rec.service_time_minutes or 0,
                "per_pallet_service_minutes":
                    rec.per_pallet_service_minutes or 0,
            })
        return stop_dicts

    def _fill_stop_dates(self, request, payload, result):
        """§5 — blank stop dates come from the selected pickup date and
        the calculated schedule; explicit dates are never overwritten."""
        pickup_date = payload.get("pickup_date") or False
        if not pickup_date:
            return
        sched_stops = []
        for card in (result.get("scenarios") or []):
            sch = card.get("schedule") or {}
            if sch.get("stops"):
                sched_stops = sch["stops"]
                break
        for i, rec in enumerate(
                request.structured_stop_ids.sorted("sequence")):
            if rec.stop_date:
                continue
            if rec.stop_type == "pickup":
                rec.sudo().write({"stop_date": pickup_date})
            elif i < len(sched_stops):
                dep_date = str(
                    sched_stops[i].get("departure") or "")[:10]
                if dep_date and dep_date[:4].isdigit():
                    rec.sudo().write({"stop_date": dep_date})

    def _enrich_payload(self, request, payload, rows, facts, warnings):
        """Post-merge payload enrichment: extraction equipment/temp, the
        resolved requested date, totals, per-segment onboard peak, cases,
        and per-stop timing."""
        stops = payload.get("stops") or []

        # Equipment + temperature from the enriched extraction.
        equipment = str(facts.get("equipment") or "").strip().lower()
        if equipment in ("reefer", "dry"):
            payload["equipment"] = equipment
            temp = facts.get("temperature_c")
            if equipment == "reefer":
                try:
                    payload["required_temperature_c"] = \
                        float(temp) if temp is not None else 15.0
                except (TypeError, ValueError):
                    payload["required_temperature_c"] = 15.0
            else:
                payload["required_temperature_c"] = False
        payload["instructions"] = facts.get("instructions") or ""

        # §2 — the posting's requested delivery date rides alongside the
        # pickup date so the scenario cards preserve BOTH dates (a same-
        # day drive with a next-day delivery needs an overnight hold,
        # never a silently pulled-forward delivery date).
        req_delivery = facts.get("requested_delivery_date")
        resolved_delivery = self._resolve_relative_date(req_delivery) \
            if req_delivery else False
        payload["requested_delivery_date"] = resolved_delivery or False
        # §6/§7 — posting facts that must survive verbatim: the posting's
        # own equipment/trailer requirement, the reference, pallet
        # dimensions and appointment requirements (a posting appointment
        # WITHOUT a time is never given an invented time — it surfaces as
        # "Appointment required — time to confirm" in the stop notes).
        payload["reference"] = str(facts.get("reference") or "").strip() \
            or False
        payload["posted_equipment"] = str(
            facts.get("posted_equipment") or "").strip() or False
        payload["dimensions"] = str(facts.get("dimensions") or "").strip() \
            or False
        payload["appointments_required"] = bool(
            facts.get("appointments_required"))

        # Requested pickup date/time from the text (panel date still wins
        # when the user picked one — scheduled_at already fed the payload).
        requested = facts.get("requested_pickup_date")
        if requested and not payload.get("pickup_date"):
            resolved = self._resolve_relative_date(requested)
            if resolved:
                payload["pickup_date"] = resolved
        payload["requested_pickup_time"] = \
            facts.get("requested_pickup_time") or False

        # Totals: the larger of the pickup side and the delivery side
        # (split quantities may live on the delivery stops; a wrong
        # pickup-side guess must not shrink the shipment).
        def _sum(field):
            pick = sum(int(s.get(field) or 0) for s in stops
                       if str(s.get("type") or "").lower()
                       in ("pickup", "origin"))
            drop = sum(int(s.get(field) or 0) for s in stops
                       if str(s.get("type") or "").lower()
                       in ("delivery", "dropoff"))
            return max(pick, drop)
        pallets = _sum("pallets")
        cases = _sum("cases")
        weight = _sum("weight_lbs")
        payload["pallets"] = pallets
        # §7: unknown weight stays UNKNOWN — a missing weight must never
        # ride as a valid zero weight into capacity/pricing.
        payload["weight_lbs"] = weight if weight > 0 else False
        payload["total_cases"] = cases
        # Per-stop service durations from Saved Location settings (same
        # authority Prema Dispatch schedules with) — the payload total
        # then equals the stop-by-stop schedule's own sum.
        svc_total = 0
        for s in stops:
            svc = int(s.get("service_time_minutes") or 0)
            if svc:
                per_p = int(s.get("per_pallet_service_minutes") or 0)
                if per_p:
                    svc += int(s.get("pallets") or 0) * per_p
                svc_total += svc
        if svc_total:
            payload["service_minutes"] = svc_total

        # Per-segment onboard peak — capacity validation uses the peak
        # load aboard each route segment, not merely the grand total.
        onboard = peak = 0
        for s in stops:
            q = int(s.get("pallets") or 0)
            kind = str(s.get("type") or "").lower()
            if kind in ("pickup", "origin"):
                onboard += q
            else:
                onboard = max(0, onboard - q)
            peak = max(peak, onboard)
        payload["peak_onboard_pallets"] = max(peak, pallets)
        request_totals = {
            "equipment": payload.get("equipment") or False,
            "instructions": payload.get("instructions") or False,
            "total_pallets": pallets,
            "total_cases": cases,
            "total_weight_lbs": weight,
        }
        if request:
            try:
                request.sudo().write(request_totals)
            except Exception:
                pass

    def _resolve_relative_date(self, value):
        """'tomorrow' / weekday names / ISO dates → ISO date (company tz)."""
        import datetime as _dt
        value = str(value or "").strip()
        if not value:
            return False
        try:
            return _dt.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
        import pytz
        tz_name = "America/Toronto"
        try:
            tz_name = self.env.user.tz or \
                self.env.company.partner_id.tz or tz_name
        except Exception:
            pass
        today = _dt.datetime.now(pytz.timezone(tz_name)).date()
        low = value.lower()
        if low == "tomorrow":
            return (today + _dt.timedelta(days=1)).isoformat()
        weekdays = ("monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday")
        if low in weekdays:
            target = weekdays.index(low)
            delta = (target - today.weekday()) % 7 or 7
            return (today + _dt.timedelta(days=delta)).isoformat()
        # Spelled-out dates from loadboard postings ("September 10, 2026",
        # "Sep 10 2026", "10 September 2026", "10th September 2026") parse
        # to the company-tz calendar date.
        cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", value,
                         flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                    "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y"):
            try:
                return _dt.datetime.strptime(cleaned, fmt).date() \
                    .isoformat()
            except ValueError:
                continue
        return False

    @api.model
    def location_search_rpc(self, term):
        """Saved Location combobox search for the panel stop editor."""
        if "logistics.estimator.bridge" not in self.env.registry:
            return []
        return self.env["logistics.estimator.bridge"].location_search_rpc(
            term or "")

    # ── Route Development week view (§13) ───────────────────────────

    @api.model
    def route_development_rpc(self, request_id, week_start=None):
        """Scheduled corridor week + development gaps for one request's
        region pair and truck. Read-only; used by the route-dev button."""
        request = self.sudo().browse(int(request_id))
        if not request.exists():
            return {"error": "Request %s not found." % request_id}
        inputs = request.inputs_json or {}
        stops = inputs.get("stops") or []
        fsa_codes = [s.get("fsa_code") for s in stops
                     if isinstance(s, dict) and s.get("fsa_code")]
        if "logistics.estimator.bridge" not in self.env.registry:
            return {"error": "Dispatch integration is offline on this "
                             "database."}
        return self.env["logistics.estimator.bridge"].route_development(
            request.vehicle_id.id, week_start, fsa_codes)

    # ── Explicit conversion action (§10.9 — human click only) ───────

    @api.model
    def action_rate_confirmation_rpc(self, request_id, scenario_key=None):
        """§10 — Create Draft Rate Confirmation for the request's customer
        with the SELECTED scenario transferred (stops, quantities,
        windows, instructions, suggested sell). This is the explicit
        conversion click: exactly ONE editable draft; nothing is sent,
        confirmed, invoiced or booked. Returns the canonical workflow
        chain (review → send → acceptance → booking) with the booking
        step unavailable until approval is recorded."""
        request = self.sudo().browse(int(request_id))
        if not request.exists():
            return {"error": "Request not found."}
        if not request.partner_id:
            return {"error": "no_partner",
                    "message": "Select the Customer on this estimate first — "
                               "a Rate Confirmation must belong to a customer."}
        lead = request.lead_id or self.sudo()._find_open_lead(
            request.partner_id)
        if not lead:
            return {"error": "no_lead",
                    "message": "No open opportunity exists for %s. Open the "
                               "customer's opportunity (or create one) and "
                               "use 'Create Draft Rate Confirmation' there — "
                               "the estimate reference is %s."
                               % (request.partner_id.name or "?", request.name)}
        try:
            if scenario_key:
                request.sudo().write({
                    "selected_scenario": str(scenario_key)})
            transfer = {}
            if "logistics.estimator.bridge" in self.env.registry:
                transfer = self.env[
                    "logistics.estimator.bridge"] \
                    .transfer_estimator_scenario_rpc(
                        request.id, scenario_key or request.selected_scenario)
                if transfer.get("quote_id"):
                    request.sudo().write({
                        "quote_id": int(transfer["quote_id"])})
            action = transfer.get("action")
            if not action and hasattr(
                    lead, "action_create_draft_rate_confirmation"):
                action = lead.action_create_draft_rate_confirmation()
            return {"ok": True, "action": action,
                    "quote_id": transfer.get("quote_id"),
                    "quote_name": transfer.get("quote_name"),
                    "quote_state": transfer.get("state"),
                    "workflow": transfer.get("workflow") or [],
                    "message": "Draft Rate Confirmation %s created for %s "
                               "(nothing sent)."
                               % (transfer.get("quote_name") or "",
                                  lead.name or "the opportunity")}
        except Exception as exc:
            return {"error": "rate_confirmation_failed",
                    "message": str(exc)[:400]}

    @api.model
    def quote_workflow_status_rpc(self, request_id):
        """Refresh the canonical quote workflow chain for the panel."""
        request = self.sudo().browse(int(request_id))
        if not request.exists() or not request.quote_id:
            return {"quote_id": (request.quote_id.id
                                 if request and request.quote_id else False),
                    "workflow": []}
        CQ = self.env["logistics.custom.quote"].sudo()
        quote = CQ.browse(request.quote_id.id)
        if not quote.exists():
            return {"quote_id": request.quote_id.id, "workflow": []}
        return {
            "quote_id": quote.id,
            "quote_name": quote.name,
            "state": quote.state,
            "workflow": [
                {"step": "review",
                 "label": "Staff reviews the draft",
                 "done": quote.state in ("reviewing", "quoted", "accepted",
                                         "converted")},
                {"step": "send",
                 "label": "Staff explicitly sends the Rate Confirmation",
                 "done": bool(quote.is_locked)},
                {"step": "acceptance",
                 "label": "Customer approval recorded against the reviewed "
                          "version",
                 "done": bool(quote.acceptance_recorded_at)},
                {"step": "booking",
                 "label": "Staff confirms the booking (capacity and "
                          "schedule re-checked at conversion)",
                 "done": quote.state == "converted",
                 "available": bool(quote.acceptance_recorded_at)},
            ],
        }

    # ── Pipeline helpers ────────────────────────────────────────────

    def _rpc_failure(self, request, message, errors=None, detail=None):
        """Structured failure dict for the estimator panel.  `message` is
        the user-facing headline (shown as the error banner), `errors` are
        per-problem bullets, `detail` is the technical trace (kept on the
        audit record and echoed small-print for support)."""
        audit_message = (detail or message) if request else message
        if request:
            try:
                request.sudo().write({
                    "state": "error",
                    "message": audit_message[:1000]})
            except Exception:
                _logger.exception("could not write error state")
        return {
            "success": False,
            "ok": False,
            "request_id": request.id if request else False,
            "state": "error",
            "error": message,
            "message": message,
            "validation_errors": list(errors) if errors else [message],
            "operational_warnings": [],
            "error_detail": detail or False,
        }

    def _collect_stops(self, request, text_message, files, vehicle):
        """Reuse the estimator's canonical text/file parsers."""
        from ..services.mapbox_service import MapboxService
        mbx = MapboxService(self.env)
        warnings = []
        all_stops = []
        estimator = self.env["premafirm.rate.estimator"].sudo().browse()
        if text_message and text_message.strip():
            t_stops, t_notes = estimator._extract_stops_from_text(
                text_message) if hasattr(
                estimator, "_extract_stops_from_text") else ([], "")
            if t_notes:
                warnings.append(str(t_notes))
            all_stops.extend(t_stops or [])
        for f in files or []:
            f_result = estimator.extract_stops_from_file_rpc(
                f.get("file_b64", ""), f.get("mimetype", ""),
                f.get("filename", ""), extra_notes="") if hasattr(
                estimator, "extract_stops_from_file_rpc") else {"error": "n/a"}
            if f_result.get("error"):
                warnings.append("File parse failed for %s: %s"
                                % (f.get("filename", ""),
                                   f_result["error"][:160]))
                continue
            for s in f_result.get("stops") or []:
                s = dict(s)
                s["type"] = s.get("type") or "delivery"
                s["_source"] = "document"
                all_stops.append(s)
            if f_result.get("notes"):
                warnings.append(str(f_result["notes"]))

        if not all_stops:
            return [], warnings

        # Coordinate + postal normalization (geocode missing stops; derive
        # the FSA from the stop's postal so the corridor card can resolve).
        for s in all_stops:
            lat, lng = float(s.get("lat") or 0) or 0.0, \
                float(s.get("lng") or 0) or 0.0
            address = str(s.get("address") or "").strip()
            if not (lat and lng) and address:
                try:
                    hits = mbx.geocode_address(address)
                    if hits:
                        s["lat"], s["lng"] = float(hits[0]["lat"]), \
                            float(hits[0]["lng"])
                except Exception:
                    pass
            if not s.get("fsa_code"):
                fsa = self._postal_fsa(
                    " ".join(filter(None, (
                        address, str(s.get("company_name") or ""),
                        str(s.get("stop_notes") or "")))))
                if fsa:
                    s["fsa_code"] = fsa
            s.setdefault("stop_notes", "")
        return all_stops, warnings

    def _validate_stop_mix(self, stops, request):
        kinds = {str(s.get("type") or "").lower() for s in stops}
        missing = []
        if not kinds & {"pickup", "origin"}:
            missing.append("pickup")
        if not kinds & {"delivery", "dropoff"}:
            missing.append("delivery")
        if missing:
            request.sudo().write({
                "state": "error",
                "message": "The request is missing a %s stop — describe "
                           "both where the freight is picked up and where "
                           "it goes." % " and ".join(missing)})
            raise ValueError(request.message)

    def _build_payload(self, request, vehicle, stops, warnings,
                       avoid_tolls, allow_cross_border, scheduled_at,
                       return_to_home, request_text="", margin_pct=None,
                       manual_distance_km=None, manual_duration_hrs=None):
        from ..services.mapbox_service import MapboxService
        from ..services.eld_adapter import EldAdapter
        mbx = MapboxService(self.env)

        home = False
        lat, lng = float(vehicle.x_home_base_lat or 0) or 0.0, \
            float(vehicle.x_home_base_lng or 0) or 0.0
        if lat and lng:
            home = {"lat": lat, "lng": lng}

        # Customer stop geometry, in message order.
        waypoints = []
        unroutable = []
        for s in stops:
            s_lat, s_lng = float(s.get("lat") or 0) or 0.0, \
                float(s.get("lng") or 0) or 0.0
            if not (s_lat and s_lng):
                unroutable.append(
                    " ".join(filter(None, (
                        s.get("company_name") or "",
                        s.get("address") or ""))) or "a stop")
                continue
            waypoints.append({"lat": s_lat, "lng": s_lng})
        if len(waypoints) < 2:
            warnings.append(
                "Routing unavailable — %s could not be located (postal/"
                "city geocoding found no match). Correct the stop in the "
                "stops editor and re-run, or use the manual road-distance "
                "override. Cards that need drive time then read "
                "\"Estimate unavailable\" — never a 0-km figure or an "
                "infeasibility." % "; ".join(unroutable or ["some stops"]))

        # Full route: optional home → first stop, customer legs, optional
        # last stop → home — ONE routing call, legs sliced per section.
        ordered_pts = []
        if home:
            ordered_pts.append(home)
        ordered_pts.extend(waypoints)
        if home and return_to_home:
            ordered_pts.append(home)
        geometry = False
        legs = []
        route_error = ""
        if len(ordered_pts) >= 2:
            # A routing refusal (unroutable stop, cross-border excluded,
            # Mapbox down) DEGRADES the request to corridor-only pricing —
            # it never turns the whole estimate into a hard error.  The
            # dispatch card engine handles an empty leg set on its own.
            try:
                geometry = mbx.get_route_multi(
                    ordered_pts,
                    max_height_ft=vehicle.x_vehicle_height_ft or 0.0,
                    gvwr_lbs=vehicle.x_gvwr_lbs or 0.0,
                    allow_cross_border=allow_cross_border,
                    avoid_tolls=avoid_tolls)
                legs = geometry.get("legs") or []
            except Exception as exc:
                route_error = str(exc)[:200]
                # One retry: when the extractor truncated a postal to its
                # FSA, its geocode can be an FSA centroid that is not on
                # the drivable grid (waterfront districts) and Mapbox then
                # refuses the whole stop set.  Re-geocode those stops at
                # city level and say so — approximate beats unavailable.
                fallback_pts = self._city_level_fallback_pts(
                    stops, home, return_to_home)
                if fallback_pts:
                    try:
                        geometry = mbx.get_route_multi(
                            fallback_pts,
                            max_height_ft=vehicle.x_vehicle_height_ft or 0.0,
                            gvwr_lbs=vehicle.x_gvwr_lbs or 0.0,
                            allow_cross_border=allow_cross_border,
                            avoid_tolls=avoid_tolls)
                        legs = geometry.get("legs") or []
                        if legs:
                            warnings.append(
                                "A stop postal was truncated in the message "
                                "and geocoded at city level — drive times "
                                "are approximate; verify the exact address "
                                "before dispatch.")
                    except Exception:
                        geometry = False
                        legs = []
                if not legs and not manual_distance_km:
                    warnings.append(
                        "Routing could not complete (%s) — dedicated drive "
                        "times/costs are unavailable; scheduled corridor "
                        "pricing may still resolve from postal codes."
                        % route_error)
        if not legs and len(ordered_pts) >= 2 and not route_error \
                and not manual_distance_km:
            warnings.append(
                "Mapbox could not route the stop set — scenario cards "
                "cannot compute drive times/costs (scheduled corridor "
                "pricing may still resolve from postal codes).")

        offset = 0
        reposition = {}
        if home:
            if legs:
                reposition = {"km": legs[0].get("distance_km"),
                              "hrs": legs[0].get("duration_hrs")}
                offset = 1
            else:
                warnings.append(
                    "Empty reposition (home → first stop) could not be "
                    "routed.")
        customer_legs = legs[offset:offset + max(len(waypoints) - 1, 0)]
        return_leg = {}
        if home and return_to_home and legs:
            tail = legs[offset + max(len(waypoints) - 1, 0):]
            if tail:
                return_leg = {"km": tail[0].get("distance_km"),
                              "hrs": tail[0].get("duration_hrs")}

        distance = sum(float(l.get("distance_km") or 0.0)
                       for l in customer_legs)
        duration = sum(float(l.get("duration_hrs") or 0.0)
                       for l in customer_legs)
        # §1 manual road-distance override — the user supplies the lane
        # distance when geocoding/routing cannot resolve the stops. Only
        # meaningful for a plain two-stop lane (no reposition/return
        # geometry is known), and every consumer sees the override flag.
        manual_route = False
        if not customer_legs and manual_distance_km:
            if len(waypoints) == 2:
                distance = max(0.0, float(manual_distance_km))
                duration = max(0.0, float(manual_duration_hrs or 0.0)) \
                    or round(distance / 70.0, 2)
                customer_legs = [{
                    "distance_km": distance, "duration_hrs": duration}]
                manual_route = {
                    "distance_km": round(distance, 1),
                    "duration_hrs": round(duration, 1)}
                warnings.append(
                    "Manual road-distance override applied: %.0f km "
                    "(drive time estimated at ~70 km/h average — no "
                    "route geometry was available). Verify the actual "
                    "distance with the driver before dispatch."
                    % distance)
            else:
                warnings.append(
                    "Manual distance override applies to a plain "
                    "two-stop lane only — %d resolvable stops were "
                    "given; correct the stop addresses instead."
                    % len(waypoints))

        def _side_sum(field, kinds):
            return sum(int(s.get(field) or 0) for s in stops
                       if str(s.get("type") or "").lower() in kinds)
        # Shipment-size authority: the larger of the pickup side and the
        # delivery side. Split quantities often live on the delivery
        # stops ("3 to A, 2 to B, 1 to C" with a bare pickup) and an
        # extractor may wrongly copy the first delivery's quantity onto
        # the pickup — max() is right for both, and equals the plain sum
        # for a single pickup delivering everything.
        pallets = max(_side_sum("pallets", ("pickup", "origin")),
                      _side_sum("pallets", ("delivery", "dropoff")))
        weight_lbs = max(
            _side_sum("weight_lbs", ("pickup", "origin")),
            _side_sum("weight_lbs", ("delivery", "dropoff")))
        if not pallets and not weight_lbs:
            warnings.append(
                "No pallet/weight quantities were extracted — capacity "
                "cannot be verified.")
        if pallets and not weight_lbs:
            warnings.append(
                "Weights were not extracted (only %d pallet positions) — "
                "payload feasibility is unverified." % pallets)
        liftgate = any(bool(s.get("liftgate")) for s in stops)
        equipment = self._proposed_equipment(vehicle, stops, request_text)
        # Reefers default to the booking flow's 15°C setpoint.
        required_temperature_c = 15.0 if equipment == "reefer" else False

        defaults = {}
        try:
            defaults = self.env["premafirm.rate.estimator"].sudo().browse() \
                .get_defaults_rpc(vehicle.id) or {}
        except Exception:
            defaults = {}
        overrides = {}
        for key in ("fuel_price_per_l", "driver_rate_per_hr",
                    "insurance_monthly", "maintenance_monthly", "margin_pct"):
            if defaults.get(key):
                overrides[key] = float(defaults[key])

        eld = EldAdapter(self.env).vehicle_eld_status(vehicle)

        service_minutes = int(
            self.env["ir.config_parameter"].sudo().get_param(
                "premafirm.estimator.service_minutes_per_stop", "20") or 20) \
            * len(stops)

        pickup_date = self._pickup_date(scheduled_at)
        # Itinerary rows — authoritative, resolved here (client renders
        # rows only).  Reposition/return are their own rows; each routable
        # customer stop carries its incoming segment (routable-leg order).
        itinerary = []
        routable_index = 0
        if reposition.get("km") is not None and home:
            itinerary.append({
                "row": "reposition", "label": "Empty reposition (home)",
                "name": vehicle.name or "truck home", "address": "",
                "qty": "", "segment_km": _num(reposition.get("km")),
                "segment_hrs": _num(reposition.get("hrs")), "seq": 0,
            })
        stop_seq = 0
        approx_stops = []
        for s in stops:
            s_lat = float(s.get("lat") or 0) or 0.0
            seg_km = seg_hrs = False
            if s_lat:
                # Segment for the (routable_index)-th routable stop is the
                # incoming customer leg; only coordinate-bearing stops were
                # routed, so only they consume a leg.
                if routable_index and \
                        routable_index - 1 < len(customer_legs):
                    seg_km = customer_legs[routable_index - 1].get(
                        "distance_km")
                    seg_hrs = customer_legs[routable_index - 1].get(
                        "duration_hrs")
                routable_index += 1
            stop_seq += 1  # strictly increasing even for unroutable stops
            kind = str(s.get("type") or "delivery").lower()
            approx_level = str(s.get("approx_level") or "street").lower()
            if approx_level in ("postal", "city"):
                approx_stops.append({
                    "name": s.get("company_name") or "",
                    "address": s.get("address") or "",
                    "fsa": str(s.get("fsa_code") or "").upper(),
                    "approx_level": approx_level,
                })
            itinerary.append({
                "row": kind, "seq": stop_seq,
                "label": "Pickup %d" % (len([i for i in itinerary
                                             if i["row"] in ("pickup",)]) + 1)
                if kind == "pickup" else
                ("Delivery %d" % (sum(1 for i in itinerary
                                      if i["row"] in ("delivery", "dropoff")) + 1)
                 if kind in ("delivery", "dropoff") else kind.title()),
                "name": s.get("company_name") or "",
                "address": s.get("address") or "",
                "fsa": str(s.get("fsa_code") or "").upper(),
                "approx_level": approx_level,
                "qty": ("%s pallet(s)" % int(s.get("pallets") or 0))
                if s.get("pallets") else
                ("%.0f lb" % float(s.get("weight_lbs") or 0.0))
                if s.get("weight_lbs") else "",
                "segment_km": seg_km, "segment_hrs": seg_hrs,
            })
        if home and return_to_home and return_leg.get("km") is not None:
            itinerary.append({
                "row": "return", "seq": stop_seq + 1,
                "label": "Return to home",
                "name": vehicle.name or "truck home", "address": "",
                "qty": "", "segment_km": _num(return_leg.get("km")),
                "segment_hrs": _num(return_leg.get("hrs")),
            })

        payload = {
            "vehicle_id": vehicle.id,
            "partner_id": request.partner_id.id if request.partner_id else 0,
            "pickup_date": pickup_date,
            # Top-level customer-leg distance (km) — the pricing-intel and
            # load-pairing services consume it for comparable filtering.
            "distance_km": round(distance, 1),
            "return_to_home": bool(home) and bool(return_to_home),
            "equipment": equipment,
            "required_temperature_c": required_temperature_c,
            "liftgate_pickup": liftgate,
            "liftgate_delivery": liftgate,
            "pallets": pallets,
            "weight_lbs": weight_lbs,
            "service_minutes": service_minutes,
            "margin_pct": (float(margin_pct) if margin_pct
                           else float(overrides.get("margin_pct") or 20.0)),
            "overrides": {k: v for k, v in overrides.items()
                          if k != "margin_pct"},
            "truck_home": home,
            "reposition_to_first_km": reposition.get("km"),
            "reposition_to_first_hrs": reposition.get("hrs"),
            "return_leg_km": return_leg.get("km"),
            "return_leg_hrs": return_leg.get("hrs"),
            "stops": [{
                "type": "pickup"
                if str(s.get("type") or "").lower() in ("pickup", "origin")
                else "delivery",
                "company_name": s.get("company_name") or "",
                "address": s.get("address") or "",
                "lat": float(s.get("lat") or 0) or 0.0,
                "lng": float(s.get("lng") or 0) or 0.0,
                "pallets": int(s.get("pallets") or 0),
                "weight_lbs": float(s.get("weight_lbs") or 0.0),
                "fsa_code": str(s.get("fsa_code") or "").upper() or "",
                "approx_level": str(s.get("approx_level")
                                     or "street").lower(),
                "liftgate": bool(s.get("liftgate")),
                "stop_notes": s.get("stop_notes") or "",
            } for s in stops],
            "route_legs": [{
                "distance_km": float(l.get("distance_km") or 0.0),
                "duration_hrs": float(l.get("duration_hrs") or 0.0),
            } for l in customer_legs],
            "driver": eld,
            "data_warnings": warnings,
            "approx_stops": approx_stops,
            "manual_route": manual_route,
            "route": {
                "distance_km": round(distance, 1),
                "duration_hrs": round(duration, 1),
                "reposition_to_first_km": _num(reposition.get("km")),
                "reposition_to_first_hrs": _num(reposition.get("hrs")),
                "return_leg_km": _num(return_leg.get("km")),
                "return_leg_hrs": _num(return_leg.get("hrs")),
                "customer_stops": len(waypoints),
                "customer_legs": [{
                    "distance_km": float(l.get("distance_km") or 0.0),
                    "duration_hrs": float(l.get("duration_hrs") or 0.0),
                } for l in customer_legs],
                # §1 — approximate stop resolutions / manual override are
                # flagged so no consumer can read a precise figure.
                "approximate": bool(approx_stops) or bool(manual_route),
                "approx_stops": approx_stops,
                "manual_distance_override": bool(manual_route),
                "itinerary": itinerary,
            },
        }
        return payload

    # ── Leaf helpers ────────────────────────────────────────────────

    def _find_open_lead(self, partner):
        """Most recent OPEN (non-terminal) lead for the partner, or False.

        crm.lead.won_status (won/lost/pending) is the canonical state field
        in this instance (100% populated); crm.stage has is_won only — there
        is NO crm.stage.is_lost, so lost-ness must not be expressed through
        the stage relation.  Archived leads are excluded by the default
        active test on search()."""
        if not partner:
            return False
        partner = partner.commercial_partner_id or partner
        Lead = self.env["crm.lead"]
        if "crm.lead" not in self.env.registry:
            return False
        domain = [("partner_id", "=", partner.id)]
        if "won_status" in Lead._fields:
            # pending = open; won/lost are terminal states.
            domain.append(("won_status", "=", "pending"))
        elif "stage_id" in Lead._fields:
            # Fallback: no stage, or a stage that is not a won stage.
            domain = domain + [
                "|",
                ("stage_id", "=", False),
                ("stage_id.is_won", "=", False),
            ]
        leads = Lead.search(domain, order="create_date desc, id desc",
                            limit=1)
        return leads[:1] if leads else False

    def _proposed_equipment(self, vehicle, stops, request_text=""):
        """Honest equipment proposal from the request text.

        Dispatch's booking flow defaults temperature-silent requests to
        reefer/15 °C when the truck can run reefer — mirror that ONLY
        when the text gives no signal.  An explicit dry/ambient/no-reefer
        signal must win over the default, and a truck without a reefer
        unit is always proposed dry (never a blocking reefer ask)."""
        hay = " ".join(filter(None, [
            request_text,
            *(str(s.get("address") or "") + " " + str(s.get("stop_notes") or "")
              for s in stops)])).lower()
        wants_reefer = any(k in hay for k in (
            "reefer", "refrigerat", "chilled", "frozen", "cold chain"))
        dry = bool(re.search(r"\b(dry|ambient)\b|dry van|dry freight|"
                             r"no reefer|heated", hay))
        if not vehicle.x_reefer:
            return "dry"
        if dry:
            return "dry"
        if wants_reefer:
            return "reefer"
        return "reefer"  # booking-flow default for temperature-silent asks

    @staticmethod
    def _sanitize_posting_text(text):
        """§7 loadboard paste hygiene — formatting clutter that carries no
        freight fact is removed BEFORE extraction so the AI never parses
        noise twice or mangles real facts with formatting symbols: lone
        'svg' tokens (vector/HTML residue), leading/trailing bullet and
        symbol glyphs, symbol-only lines, and exact duplicate lines
        (repeated date fragments) are dropped. Factual lines are never
        altered."""
        symbols = (r"—–‒⁃•·●"
                   r"▪▫◦❥›»‣✦"
                   r"✱✿★☆✧\*#>")
        lines = []
        for raw in re.split(r"\r?\n", str(text or "")):
            line = re.sub(r"(?i)\bsvg\b", " ", raw)
            line = re.sub(r"^[\s%s]+|[\s%s]+$" % (symbols, symbols),
                          "", line)
            line = re.sub(r"\s+", " ", line).strip()
            if not line or re.match(r"^[\s%s]+$" % symbols, line):
                continue
            if line in lines:
                continue  # repeated fragment — keep each fact once
            lines.append(line)
        cleaned = "\n".join(lines)
        return cleaned if cleaned.strip() else str(text or "")

    @staticmethod
    def _postal_fsa(text):
        m = _FSA_RE.search(text or "")
        if m:
            return (m.group(1) + m.group(2)).upper()[:3]
        m = _US_ZIP_RE.search(text or "")
        if m:
            return m.group(1)[:3]
        # Bare-FSA fallback: extractors sometimes truncate a postal to its
        # first three characters ("M5V 1W1" -> "M5V"); the corridor card and
        # pairing still resolve from the FSA alone.  "ON"/"QC" style tokens
        # never match the X1X shape, so false positives are unlikely.
        m = _FSA_BARE_RE.search(text or "")
        return (m.group(1) if m else "").upper()

    def _city_level_fallback_pts(self, stops, home, return_to_home):
        """Ordered point list with city-level coordinates for every stop
        whose address lost its full postal — only when the primary route
        probe was refused.  Returns [] unless at least one stop changed, so
        the normal case never pays an extra geocode."""
        from ..services.mapbox_service import MapboxService
        mbx = MapboxService(self.env)
        pts = []
        changed = 0
        for s in stops:
            s_lat, s_lng = float(s.get("lat") or 0) or 0.0, \
                float(s.get("lng") or 0) or 0.0
            address = str(s.get("address") or "").strip()
            if (s_lat and s_lng) and address and \
                    not _FSA_RE.search(address + " "
                                       + str(s.get("stop_notes") or "")):
                try:
                    hits = mbx.geocode_address(address)
                except Exception:
                    hits = []
                hit = next((h for h in hits
                            if "Canada" in h.get("place_name", "")), None)
                if not hit and hits:
                    hit = hits[0]
                if hit:
                    s_lat, s_lng = float(hit["lat"]), float(hit["lng"])
                    changed += 1
            if s_lat and s_lng:
                pts.append({"lat": s_lat, "lng": s_lng})
        if not changed or len(pts) < 2:
            return []
        ordered = []
        if home:
            ordered.append(home)
        ordered.extend(pts)
        if home and return_to_home:
            ordered.append(home)
        return ordered if len(ordered) >= 2 else []

    @staticmethod
    def _pickup_date(value):
        if not value:
            return False
        if isinstance(value, datetime.datetime):
            return value.date().isoformat()
        if isinstance(value, datetime.date):
            return value.isoformat()
        return str(value)[:10] or False

    def _engine_version(self):
        module = self.env["ir.module.module"].search(
            [("name", "=", _MODULE_NAME)], limit=1)
        return module.latest_version if module else ""

    @staticmethod
    def _default_name():
        return "EST-%s" % (
            datetime.datetime.utcnow().strftime("%y%m%d-%H%M%S"))


def _num(value):
    if value is None:
        return False
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return False
