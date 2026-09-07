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
    distance_km = fields.Float(string="Distance (km)", digits=0)
    dispatch_online = fields.Boolean(string="Dispatch bridge online",
                                     readonly=True)
    message = fields.Text(string="Message")

    # ── Entry point (called by the reworked estimator panel) ─────────

    def estimate_scenarios_rpc(self, vehicle_id, text_message="", files=None,
                               partner_id=0, avoid_tolls=True,
                               allow_cross_border=False, scheduled_at=None,
                               return_to_home=True):
        """One message → one consolidated response (scenarios + itinerary
        + intel + pairing).  Creates ONLY this audit record."""
        request = self.env["premafirm.estimator.scenario.request"]
        try:
            vehicle = self.env["fleet.vehicle"].sudo().browse(int(vehicle_id))
            if not vehicle.exists():
                return {"error": "Truck %s not found." % vehicle_id}
            request = self.sudo().create({
                "vehicle_id": vehicle.id,
                "partner_id": int(partner_id or 0) or False,
                "engine_version": self._engine_version(),
                "state": "draft",
            })

            stops, warnings = self._collect_stops(
                request, text_message, files or [], vehicle)
            if not stops:
                request.sudo().write({
                    "state": "error",
                    "message": "Could not detect any stops from the provided "
                               "input. Please check the message or file."})
                return {"error": request.message, "request_id": request.id,
                        "state": "error"}
            self._validate_stop_mix(stops, request)

            payload = self._build_payload(
                request, vehicle, stops, warnings, avoid_tolls,
                allow_cross_border, scheduled_at, return_to_home)
            request.inputs_json = payload

            bridge_ok = "logistics.estimator.bridge" in self.env.registry
            response = {
                "ok": False, "request_id": request.id, "state": "error",
                "dispatch_online": bridge_ok, "route": payload["route"],
                "warnings": warnings, "scenarios": [], "intel": {},
                "pairing": {}, "lead_id": False,
            }
            if not bridge_ok:
                response["ok"] = True
                response["state"] = "computed"
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
            if result.get("error"):
                response["state"] = "computed"
                response["ok"] = True
                response["message"] = (
                    "Dispatch scenario engine could not answer: %s"
                    % result.get("message", "unknown error"))
                warnings.append(response["message"])
            else:
                response["ok"] = True
                response["state"] = "computed"
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
            # the caller gets the request_id back to find it.
            _logger.exception("estimate_scenarios_rpc failed")
            if request:
                try:
                    request.sudo().write({
                        "state": "error", "message": str(exc)[:400]})
                    request.response_json = {
                        "state": "error", "error": str(exc)[:400]}
                except Exception:
                    _logger.exception("could not write error state")
            return {"error": str(exc)[:400],
                    "request_id": request.id if request else False,
                    "state": "error"}

    # ── Route Development week view (§13) ───────────────────────────

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

    def action_rate_confirmation_rpc(self, request_id):
        """Draft Rate Confirmation for the request's customer. This is the
        explicit conversion click: it reuses the CRM lead bridge
        (action_create_draft_rate_confirmation) which drafts UNPRICED and
        never sends; every number stays on this request record for the
        human to apply."""
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
        if not hasattr(lead, "action_create_draft_rate_confirmation"):
            return {"error": "crm_bridge_offline",
                    "message": "The CRM rate-confirmation bridge is not "
                               "loaded on this database."}
        try:
            action = lead.action_create_draft_rate_confirmation()
            return {"ok": True, "action": action,
                    "message": "Draft rate confirmation for %s surfaced "
                               "(unpriced by design — apply the suggested "
                               "sell above through the dispatch pricing "
                               "flow)." % (lead.name or "the opportunity")}
        except Exception as exc:
            return {"error": "rate_confirmation_failed",
                    "message": str(exc)[:400]}

    # ── Pipeline helpers ────────────────────────────────────────────

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
                       return_to_home):
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
        for s in stops:
            s_lat, s_lng = float(s.get("lat") or 0) or 0.0, \
                float(s.get("lng") or 0) or 0.0
            if not (s_lat and s_lng):
                continue
            waypoints.append({"lat": s_lat, "lng": s_lng})
        if len(waypoints) < 2:
            warnings.append(
                "Fewer than two routable stops (coordinates missing) — "
                "drive times/costs are unavailable until the addresses "
                "geocode.")

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
                if not legs:
                    warnings.append(
                        "Routing could not complete (%s) — dedicated drive "
                        "times/costs are unavailable; scheduled corridor "
                        "pricing may still resolve from postal codes."
                        % route_error)
        if not legs and len(ordered_pts) >= 2 and not route_error:
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

        pallets = sum(int(s.get("pallets") or 0) for s in stops
                      if str(s.get("type") or "").lower() in
                      ("pickup", "origin"))
        weight_lbs = sum(float(s.get("weight_lbs") or 0.0) for s in stops
                         if str(s.get("type") or "").lower() in
                         ("pickup", "origin"))
        if not pallets and not weight_lbs:
            warnings.append(
                "No pallet/weight quantities were extracted — capacity "
                "cannot be verified.")
        if pallets and not weight_lbs:
            warnings.append(
                "Weights were not extracted (only %d pallet positions) — "
                "payload feasibility is unverified." % pallets)
        liftgate = any(bool(s.get("liftgate")) for s in stops)
        equipment = "reefer" if (
            vehicle.x_reefer and not self._reefer_explicitly_off(stops)
        ) else "dry"
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
            "margin_pct": float(overrides.get("margin_pct") or 20.0),
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
                "liftgate": bool(s.get("liftgate")),
                "stop_notes": s.get("stop_notes") or "",
            } for s in stops],
            "route_legs": [{
                "distance_km": float(l.get("distance_km") or 0.0),
                "duration_hrs": float(l.get("duration_hrs") or 0.0),
            } for l in customer_legs],
            "driver": eld,
            "data_warnings": warnings,
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
                "itinerary": itinerary,
            },
        }
        return payload

    # ── Leaf helpers ────────────────────────────────────────────────

    def _find_open_lead(self, partner):
        if not partner:
            return False
        partner = partner.commercial_partner_id or partner
        Lead = self.env["crm.lead"]
        if "crm.lead" not in self.env.registry:
            return False
        domain = [("partner_id", "=", partner.id)]
        if "stage_id" in Lead._fields:
            domain.append(["|", ("stage_id.is_won", "=", False),
                           ("stage_id", "=", False)])
            domain.append(["|", ("stage_id.is_lost", "=", False),
                           ("stage_id", "=", False)])
        leads = Lead.search(domain, order="create_date desc, id desc",
                            limit=1)
        return leads[:1] if leads else False

    def _reefer_explicitly_off(self, stops):
        return any("reefer" in str(s.get("stop_notes") or "").lower()
                   and "no reefer" in str(s.get("stop_notes") or "").lower()
                   for s in stops)

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
