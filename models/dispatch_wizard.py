import base64
import json
import logging
import re
from datetime import datetime

from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


class PremafirmDispatchWizardStop(models.TransientModel):
    _name = "premafirm.dispatch.wizard.stop"
    _description = "Dispatch Wizard — Stop"
    _order = "wizard_id, sequence"

    wizard_id      = fields.Many2one("premafirm.dispatch.wizard", ondelete="cascade")
    sequence       = fields.Integer(default=10)
    stop_type      = fields.Selection([
        ("origin",   "Origin"),
        ("pickup",   "Pickup"),
        ("delivery", "Delivery"),
        ("return",   "Return"),
    ], default="pickup", required=True)
    is_system      = fields.Boolean(default=False)
    address        = fields.Char()
    lat            = fields.Float(digits=(10, 6))
    lng            = fields.Float(digits=(10, 6))
    scheduled_time = fields.Datetime()
    notes          = fields.Char()
    pallets        = fields.Integer()
    liftgate       = fields.Boolean()

    def as_dict(self):
        return {
            "type":           self.stop_type,
            "is_system":      self.is_system,
            "address":        self.address or "",
            "lat":            self.lat,
            "lng":            self.lng,
            "scheduled_time": self.scheduled_time.isoformat() if self.scheduled_time else None,
            "notes":          self.notes or "",
            "pallets":        self.pallets or 0,
            "liftgate":       self.liftgate,
        }


class PremafirmDispatchWizard(models.TransientModel):
    _name = "premafirm.dispatch.wizard"
    _description = "Dispatch Wizard — AI Job Planner"

    invoice_id     = fields.Many2one("account.move", string="Invoice", required=True,
                                      ondelete="cascade")
    job_sequence   = fields.Integer(string="Job #", default=1)
    scheduled_date = fields.Date(string="Job Date")
    job_day_ref    = fields.Char(string="Job Label", compute="_compute_job_day_ref", store=True)
    vehicle_id     = fields.Many2one("fleet.vehicle", string="Truck (optional — AI will suggest)")

    route_file     = fields.Binary(string="Route List / Load Sheet")
    route_filename = fields.Char()
    notes          = fields.Text(string="Job Notes")

    ai_plan_result      = fields.Text(string="AI Plan Summary")
    dispatcher_notes    = fields.Text(string="Dispatcher Notes / Edits",
                                       help="Add corrections or extra instructions here after reviewing the AI plan.")
    ai_stops            = fields.One2many("premafirm.dispatch.wizard.stop", "wizard_id",
                                           string="Planned Stops")
    extraction_status   = fields.Char(string="File Extraction", readonly=True)

    status         = fields.Selection([
        ("draft",     "Draft"),
        ("analyzed",  "AI Plan Ready"),
        ("error",     "Error"),
    ], default="draft")
    error_message  = fields.Char(readonly=True)

    @api.depends("scheduled_date")
    def _compute_job_day_ref(self):
        for rec in self:
            if rec.scheduled_date:
                rec.job_day_ref = rec.scheduled_date.strftime("%A %B %d %Y")
            else:
                rec.job_day_ref = f"Job {rec.job_sequence}"

    def _call_openai(self, system, user, max_tokens=1000):
        """AI call via OpenAI."""
        from ..services.openai_utils import openai_chat, DEFAULT_MODEL
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("openai.api_key") or ICP.get_param("prema_ai.api_key")
        if not api_key:
            raise ValueError("OpenAI API key not configured (openai.api_key).")
        model = ICP.get_param("prema_ai.fast_model") or DEFAULT_MODEL
        return openai_chat(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=max_tokens,
            api_key=api_key,
            model=model,
            timeout=45,
        )

    @staticmethod
    def _parse_json_safe(text):
        """Robustly extract a JSON object from AI output that may contain markdown or prose."""
        if not text:
            return None
        text = text.strip()

        # 1. Try direct parse first (Claude sometimes returns clean JSON)
        try:
            return json.loads(text)
        except Exception:
            pass

        # 2. Strip ```json ... ``` or ``` ... ``` fences
        fence = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        fence = re.sub(r'\s*```\s*$', '', fence)
        try:
            return json.loads(fence)
        except Exception:
            pass

        # 3. Find the first { and match its closing } by counting braces
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        return None

    @staticmethod
    def _parse_stop_time(raw_t, fallback_date=None):
        """Parse a stop time string to a datetime, optionally combining with a date."""
        if not raw_t:
            return None
        raw_t = str(raw_t).strip()
        # ISO formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(raw_t, fmt)
            except Exception:
                pass
        # Time-only like "08:00", "8:00 AM"
        if fallback_date:
            for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
                try:
                    t = datetime.strptime(raw_t, fmt)
                    return fallback_date.replace(hour=t.hour, minute=t.minute, second=0)
                except Exception:
                    pass
        return None

    def _build_stops_from_data(self, stops_data):
        """Convert a list of stop dicts (from extraction or AI) into wizard stop records."""
        self.ai_stops.unlink()
        if not stops_data:
            return
        fallback_date = datetime.combine(self.scheduled_date, datetime.min.time()) \
            if self.scheduled_date else None

        stop_vals = []
        for i, s in enumerate(stops_data):
            # Normalise stop type: delivery, dropoff, drop → "delivery"; pickup → "pickup"
            raw_type = (s.get("type") or s.get("stop_type") or "delivery").lower()
            if raw_type in ("delivery", "dropoff", "drop", "drop-off", "drop off"):
                stop_type = "delivery"
            elif raw_type in ("pickup", "pick-up", "pick up"):
                stop_type = "pickup"
            else:
                # Default: first stop is pickup, rest are delivery
                stop_type = "pickup" if i == 0 else "delivery"

            sched = self._parse_stop_time(
                s.get("scheduled_time") or s.get("time_window_start") or s.get("stop_date"),
                fallback_date,
            )
            # Build address from parts if no full address given
            address = s.get("address") or ""
            if not address:
                parts = [s.get("company_name", ""), s.get("street", ""),
                         s.get("city", ""), s.get("province", "")]
                address = ", ".join(p for p in parts if p)

            stop_vals.append({
                "wizard_id":      self.id,
                "sequence":       (i + 1) * 10,
                "stop_type":      stop_type,
                "is_system":      bool(s.get("is_system")),
                "address":        address,
                "lat":            float(s.get("lat") or 0),
                "lng":            float(s.get("lng") or 0),
                "scheduled_time": sched,
                "notes":          s.get("notes") or s.get("stop_notes") or s.get("special_instructions") or "",
                "pallets":        int(s.get("pallets") or 0),
                "liftgate":       bool(s.get("liftgate")),
            })
        if stop_vals:
            self.env["premafirm.dispatch.wizard.stop"].create(stop_vals)

    def action_analyze(self):
        """
        Phase 1 — extract stops from uploaded file (free, local parser + Claude vision).
        Phase 2 — Claude fills in missing times/dates/notes from free-text notes.
        Extracted stops are ALWAYS preserved; AI only enriches, never discards.
        """
        self.ensure_one()
        Estimator = self.env["premafirm.rate.estimator"].sudo()

        extracted_stops = []
        extracted_notes = ""
        extraction_error = ""

        # ── Phase 1: extract from uploaded file ───────────────────────────
        if self.route_file:
            # Odoo Binary fields return raw bytes — must base64-encode for the RPC
            raw_bytes = self.route_file
            if isinstance(raw_bytes, bytes):
                file_b64 = base64.b64encode(raw_bytes).decode("utf-8")
            else:
                file_b64 = raw_bytes  # already base64 string

            fname = (self.route_filename or "upload").lower()
            if fname.endswith(".pdf"):
                mimetype = "application/pdf"
            elif fname.endswith(".png"):
                mimetype = "image/png"
            else:
                mimetype = "image/jpeg"

            _logger.info("Dispatch wizard: extracting stops from %s (%s)", fname, mimetype)
            extract_result = Estimator.extract_stops_from_file_rpc(
                file_b64=file_b64,
                mimetype=mimetype,
                filename=self.route_filename or "upload",
                extra_notes=self.notes or "",
            )
            _logger.info("Dispatch wizard extraction result: error=%s stops=%s",
                         extract_result.get("error"), len(extract_result.get("stops") or []))
            if extract_result.get("error"):
                extraction_error = extract_result["error"]
                extraction_status = f"⚠ File extraction failed: {extraction_error[:80]}"
            else:
                extracted_stops = extract_result.get("stops", [])
                extracted_notes = extract_result.get("notes", "")
                parser = extract_result.get("parser_used", "local")
                extraction_status = f"✓ Extracted {len(extracted_stops)} stop(s) from file [{parser}]"
        else:
            extraction_status = "No file uploaded — using notes only"

        self.write({"extraction_status": extraction_status})

        # ── Phase 2: AI enrichment ─────────────────────────────────────────
        # Build what we have so far (from file extraction + user notes)
        date_str = self.scheduled_date.strftime("%A %B %d %Y") if self.scheduled_date else "today"
        combined_notes = "\n".join(filter(None, [self.notes or "", extracted_notes]))
        has_stops = bool(extracted_stops)

        if has_stops:
            # We already have stops — ask Claude only to fill in missing times/dates/details
            prompt = (
                f"Job date: {date_str}\n\n"
                f"These stops were extracted from the route document (DO NOT remove or reorder them):\n"
                f"{json.dumps(extracted_stops, indent=2)}\n\n"
                f"Additional dispatcher notes: {combined_notes or '(none)'}\n\n"
                "Your task — fix and enrich the stops list ONLY:\n"
                "1. Correct stop type: deliveries should be type='delivery', pickups type='pickup'\n"
                "2. Fill in scheduled_time (ISO format) where missing, using the job date and any time hints in notes or addresses\n"
                "3. Fill in missing address details from context clues\n"
                "4. Preserve all existing pallets, weight, liftgate, notes values\n"
                "5. Write a 1-2 sentence job summary\n\n"
                "Return ONLY this JSON (no markdown, no explanation):\n"
                '{"summary": "...", "stops": [{"type": "pickup|delivery", "address": "...", '
                '"scheduled_time": "YYYY-MM-DDTHH:MM", "notes": "...", "pallets": 0, "liftgate": false}]}'
            )
        else:
            # No file or extraction failed — plan from notes alone
            prompt = (
                f"Job date: {date_str}\n\n"
                f"Dispatcher notes: {combined_notes or '(no details provided)'}\n\n"
                "Extract or infer the full stop list for this freight job.\n"
                "- First stop: pickup location\n"
                "- Remaining stops: delivery locations\n"
                "- Use job date for scheduled_time; infer realistic times if mentioned\n\n"
                "Return ONLY this JSON (no markdown, no explanation):\n"
                '{"summary": "...", "stops": [{"type": "pickup|delivery", "address": "...", '
                '"scheduled_time": "YYYY-MM-DDTHH:MM", "notes": "...", "pallets": 0, "liftgate": false}]}'
            )

        plan = None
        ai_error = ""
        try:
            raw = self._call_openai(
                system=(
                    "You are a freight dispatch assistant. "
                    "Return ONLY valid JSON — no markdown fences, no explanation text."
                ),
                user=prompt,
                max_tokens=1500,
            )
            plan = self._parse_json_safe(raw)
            if plan is None:
                ai_error = f"Could not parse AI response as JSON. Raw: {raw[:200]}"
        except Exception as e:
            ai_error = str(e)
            _logger.warning("Dispatch wizard AI enrichment failed: %s", e)

        # ── Phase 3: populate stops ────────────────────────────────────────
        if plan and plan.get("stops"):
            self._build_stops_from_data(plan["stops"])
            summary = plan.get("summary", "")
        elif extracted_stops:
            # AI failed but we have file-extracted stops — use them directly
            self._build_stops_from_data(extracted_stops)
            summary = f"Stops extracted from uploaded document. {extraction_error or ai_error}"
        else:
            err_parts = [p for p in [extraction_error, ai_error] if p]
            self.write({
                "status": "error",
                "error_message": "; ".join(err_parts) or "No stops could be extracted. Upload a route sheet or add more detail in the notes.",
            })
            return self._reopen()

        if not summary:
            summary = f"Job planned for {date_str}. {len(self.ai_stops)} stop(s) loaded."
        if extracted_notes and extracted_notes not in summary:
            summary = f"{summary}\n\nRoute notes: {extracted_notes}"
        if ai_error and extracted_stops:
            summary = f"{summary}\n\n⚠ AI enrichment skipped ({ai_error[:120]}). Please review times manually."

        self.write({
            "ai_plan_result":   summary,
            "notes":            self.notes or extracted_notes or "",
            "dispatcher_notes": self.dispatcher_notes or "",
            "status":           "analyzed",
        })
        return self._reopen()

    def action_create_job(self):
        """Create estimator, dispatch to Fleetbase, and add calendar event."""
        self.ensure_one()
        if self.status != "analyzed":
            raise exceptions.UserError("Run 'Analyze with AI' first to generate a job plan.")
        if not self.ai_stops:
            raise exceptions.UserError("No stops defined. Please add at least 2 stops.")

        Estimator = self.env["premafirm.rate.estimator"].sudo()

        stops_list = [s.as_dict() for s in self.ai_stops.sorted("sequence")]

        # Build vehicle defaults
        vehicle_id = self.vehicle_id.id if self.vehicle_id else None
        defaults = {}
        if vehicle_id:
            defaults = Estimator.get_defaults_rpc(vehicle_id)

        # Calculate scheduled_at from first non-system stop's scheduled_time or scheduled_date
        scheduled_at = None
        for s in self.ai_stops.sorted("sequence"):
            if s.scheduled_time:
                scheduled_at = s.scheduled_time.strftime("%Y-%m-%d %H:%M:%S")
                break
        if not scheduled_at and self.scheduled_date:
            scheduled_at = f"{self.scheduled_date} 08:00:00"

        # If no vehicle selected, try to find the best one
        if not vehicle_id:
            plan = Estimator.suggest_dispatch_plan_rpc(
                stops=stops_list,
                scheduled_at=scheduled_at,
                notes=self.notes,
            )
            if plan.get("error"):
                raise exceptions.UserError(f"Could not find a suitable truck: {plan['error']}")
            vehicle_id = plan.get("selected_truck", {}).get("id")
            estimate_data = plan.get("estimate", {})
            estimate_id = estimate_data.get("estimate_id")
        else:
            estimate = Estimator.calculate_estimate(
                vehicle_id,
                stops_list,
                scheduled_at=scheduled_at,
                notes=self.notes,
                **{k: v for k, v in defaults.items()
                   if k in ("fuel_price_per_l", "driver_rate_per_hr",
                            "insurance_monthly", "maintenance_monthly", "margin_pct")},
            )
            if estimate.get("error"):
                raise exceptions.UserError(f"Could not calculate estimate: {estimate['error']}")
            estimate_id = estimate.get("estimate_id")

        if not estimate_id:
            raise exceptions.UserError("Estimate could not be created. Check stop addresses and try again.")

        # Link estimate to this invoice
        estimator_rec = Estimator.browse(estimate_id)
        job_day_ref = self.job_day_ref or (self.scheduled_date.strftime("%A %B %d %Y") if self.scheduled_date else f"Job {self.job_sequence}")
        estimator_rec.write({
            "invoice_id":    self.invoice_id.id,
            "job_day_ref":   job_day_ref,
            "job_sequence":  self.job_sequence,
        })

        # Add system origin/return stops
        estimator_rec.add_system_stops()

        # Dispatch to Fleetbase
        dispatch_result = estimator_rec.action_dispatch_to_fleetbase()
        dispatch_success = not (isinstance(dispatch_result, dict) and dispatch_result.get("params", {}).get("type") == "warning")

        # Create Odoo Calendar event
        cal_start = None
        cal_stop = None
        if self.scheduled_date:
            cal_start = datetime.combine(self.scheduled_date, datetime.min.time()).replace(hour=8)
            cal_stop  = datetime.combine(self.scheduled_date, datetime.min.time()).replace(hour=17)
        if cal_start:
            truck = self.env["fleet.vehicle"].sudo().browse(vehicle_id) if vehicle_id else None
            truck_name = truck.name if truck and truck.exists() else "Unknown Truck"
            event = self.env["calendar.event"].sudo().create({
                "name":        f"Dispatch — {job_day_ref} — {truck_name}",
                "start":       cal_start.strftime("%Y-%m-%d %H:%M:%S"),
                "stop":        cal_stop.strftime("%Y-%m-%d %H:%M:%S"),
                "description": (
                    f"Invoice: {self.invoice_id.name}\n"
                    f"Truck: {truck_name}\n"
                    f"Job: {job_day_ref}\n\n"
                    f"{self.ai_plan_result or ''}\n\n"
                    f"Stops:\n" + "\n".join(f"  {s.stop_type.upper()}: {s.address or '?'}" for s in self.ai_stops.sorted("sequence"))
                ),
                "user_id": self.env.uid,
            })
            estimator_rec.write({"calendar_event_id": event.id})

        msg_parts = [f"Job '{job_day_ref}' created and linked to this invoice."]
        if dispatch_success:
            msg_parts.append("Dispatched to Fleetbase.")
        else:
            msg_parts.append("Could not dispatch to Fleetbase — dispatch manually from the job record.")
        if cal_start:
            msg_parts.append("Calendar event created.")

        # Combine notes (original + dispatcher edits)
        final_notes = "\n".join(filter(None, [self.notes or "", self.dispatcher_notes or ""]))
        if final_notes != (self.notes or ""):
            estimator_rec.write({"notes": final_notes})

        # Post note to invoice chatter
        self.invoice_id.message_post(body=" ".join(msg_parts))

        # ── ML Learning: save this confirmed job as knowledge ──────────────
        self._save_dispatch_to_ml(estimator_rec, stops_list, job_day_ref, final_notes)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Job Created",
                "message": " ".join(msg_parts),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def _save_dispatch_to_ml(self, estimator_rec, stops_list, job_day_ref, notes):
        """Save this confirmed dispatch job to the ML knowledge base."""
        try:
            truck_name = estimator_rec.vehicle_id.name if estimator_rec.vehicle_id else ""
            input_ctx = (
                f"Dispatch job: {job_day_ref} | "
                f"Truck: {truck_name} | "
                f"Stops: {len(stops_list)} | "
                f"Route: {estimator_rec.origin_address or ''} → {estimator_rec.destination_address or ''} | "
                f"Notes: {(notes or '')[:120]}"
            )
            good_output = json.dumps({
                "job_day_ref":    job_day_ref,
                "vehicle_id":     estimator_rec.vehicle_id.id if estimator_rec.vehicle_id else None,
                "vehicle_name":   truck_name,
                "stops":          stops_list,
                "distance_km":    estimator_rec.distance_km,
                "suggested_rate": estimator_rec.suggested_rate,
                "notes":          notes or "",
                "invoice_id":     self.invoice_id.id,
            }, default=str)
            self.env["premafirm.ml.knowledge"].sudo().create({
                "knowledge_type": "rate_quote",
                "input_context":  input_ctx,
                "good_output":    good_output,
                "origin":         "approved",
                "weight":         2.0,
            })
        except Exception as e:
            _logger.warning("Could not save dispatch to ML: %s", e)

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
