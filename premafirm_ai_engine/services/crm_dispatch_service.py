import logging
import math
import re
from datetime import datetime, time, timedelta

from odoo import fields
from odoo.exceptions import UserError

from .ai_extraction_service import AIExtractionService
from .mapbox_service import MapboxService
from .pricing_engine import PricingEngine

_logger = logging.getLogger(__name__)


class CRMDispatchService:
    """CRM integration layer that orchestrates AI extraction, routing, pricing, and quote generation."""

    def __init__(self, env):
        self.env = env
        self.ai_service = AIExtractionService(env)
        self.mapbox_service = MapboxService(env)
        self.pricing_engine = PricingEngine(env)

    def _normalize_stop_values(self, extracted_stops, total_weight_lbs=None, total_pallets=None, default_service_type="dry"):
        stops = []
        for seq, stop in enumerate(extracted_stops or [], 1):
            stop_type = stop.get("stop_type") if stop.get("stop_type") in ("pickup", "delivery") else None
            address = (stop.get("address") or "").strip()
            if not stop_type or not address:
                continue

            requested = stop.get("window_start") or stop.get("scheduled_datetime")
            if stop_type == "pickup" and not requested:
                requested = datetime.combine(fields.Date.today(), time(9, 0))

            stop_country = (stop.get("country") or "").strip()
            if not stop_country:
                up = address.upper()
                stop_country = "USA" if ("UNITED STATES" in up or ", US" in up or " USA" in up) else "Canada"

            stops.append(
                {
                    "sequence": seq,
                    "stop_type": stop_type,
                    "address": address,
                    "country": stop_country,
                    "pallets": stop.get("pallets"),
                    "weight_lbs": stop.get("weight_lbs"),
                    "service_type": stop.get("service_type") or default_service_type,
                    "pickup_window_start": stop.get("window_start") if stop_type == "pickup" else None,
                    "pickup_window_end": stop.get("window_end") if stop_type == "pickup" else None,
                    "delivery_window_start": stop.get("window_start") if stop_type == "delivery" else None,
                    "delivery_window_end": stop.get("window_end") if stop_type == "delivery" else None,
                    "scheduled_datetime": requested,
                    "special_instructions": stop.get("special_instructions"),
                }
            )

        if not stops:
            return []

        missing_weight = [s for s in stops if not s.get("weight_lbs")]
        if total_weight_lbs and missing_weight:
            per_stop_weight = float(total_weight_lbs) / len(stops)
            for stop in missing_weight:
                stop["weight_lbs"] = round(per_stop_weight, 2)

        missing_pallets = [s for s in stops if not s.get("pallets")]
        if total_pallets and missing_pallets:
            per_stop_pallets = max(1, math.ceil(float(total_pallets) / len(stops)))
            for stop in missing_pallets:
                stop["pallets"] = per_stop_pallets
        elif missing_pallets:
            for stop in missing_pallets:
                weight = float(stop.get("weight_lbs") or 0.0)
                stop["pallets"] = max(1, math.ceil(weight / 1800.0)) if weight else 0

        for stop in stops:
            stop["pallets"] = int(stop.get("pallets") or 0)
            stop["weight_lbs"] = float(stop.get("weight_lbs") or 0.0)
        return stops

    def _compute_break_hours(self, segment_drive_hours, state):
        break_hours = 0.0
        before = state.get("since_major", 0.0)
        after = before + segment_drive_hours

        if before < 4 <= after:
            break_hours += 0.25  # 15 min
        if before < 8 <= after:
            break_hours += 0.75  # 45 min
            state["since_major"] = max(after - 8, 0.0)
        else:
            state["since_major"] = after

        if state["since_major"] >= 3.0:
            break_hours += 0.1667  # 10 min
            state["since_major"] = 0.0

        return break_hours

    def _compute_stop_schedule(self, ordered_stops, segments):
        first_pickup = ordered_stops.filtered(lambda s: s.stop_type == "pickup")[:1]
        if first_pickup:
            if first_pickup.pickup_window_start:
                first_pickup.scheduled_datetime = first_pickup.pickup_window_start
            elif not first_pickup.scheduled_datetime:
                first_pickup.scheduled_datetime = datetime.combine(fields.Date.today(), time(9, 0))

        if first_pickup and first_pickup.scheduled_datetime:
            first_leg_hours = float(segments[0].get("drive_hours") or 0.0) if segments else 0.0
            leave_yard_at = first_pickup.scheduled_datetime - timedelta(hours=first_leg_hours, minutes=25)
        else:
            leave_yard_at = fields.Datetime.now()

        running_dt = leave_yard_at
        break_state = {"since_major": 0.0}
        total_distance = 0.0
        total_hours = 0.0

        for idx, stop in enumerate(ordered_stops):
            segment = segments[idx] if idx < len(segments) else {}
            base_drive_hours = float(segment.get("drive_hours") or 0.0)
            break_hours = self._compute_break_hours(base_drive_hours, break_state)
            effective_drive_hours = base_drive_hours + break_hours
            total_distance += float(segment.get("distance_km") or 0.0)
            total_hours += effective_drive_hours

            if not stop.scheduled_datetime:
                if stop.stop_type == "pickup" and stop.pickup_window_start:
                    stop.scheduled_datetime = stop.pickup_window_start
                elif stop.stop_type == "delivery" and stop.delivery_window_start:
                    stop.scheduled_datetime = stop.delivery_window_start
                else:
                    stop.scheduled_datetime = running_dt + timedelta(hours=effective_drive_hours)

            estimated_arrival = running_dt + timedelta(hours=effective_drive_hours)
            if stop.stop_type == "delivery" and stop.delivery_window_start and estimated_arrival < stop.delivery_window_start:
                estimated_arrival = stop.delivery_window_start
            stop.write(
                {
                    "distance_km": float(segment.get("distance_km") or 0.0),
                    "drive_hours": effective_drive_hours,
                    "scheduled_datetime": stop.scheduled_datetime,
                    "estimated_arrival": estimated_arrival,
                    "map_url": segment.get("map_url"),
                }
            )
            running_dt = estimated_arrival

        return leave_yard_at, total_distance, total_hours

    def _create_calendar_booking(self, lead):
        if not (lead.assigned_vehicle_id and lead.departure_time and lead.total_distance_km):
            return

        driver_partner = lead.assigned_vehicle_id.driver_id.partner_id
        if not driver_partner:
            return

        last_eta = max(lead.dispatch_stop_ids.mapped("estimated_arrival") or [lead.departure_time])
        overlap_domain = [
            ("partner_ids", "in", driver_partner.id),
            ("start", "<", last_eta),
            ("stop", ">", lead.departure_time),
        ]
        overlap = self.env["calendar.event"].search(overlap_domain, limit=1)
        if overlap:
            raise UserError("Driver already has an overlapping booking in Calendar.")

        existing = self.env["calendar.event"].search(
            [
                ("res_model", "=", "crm.lead"),
                ("res_id", "=", lead.id),
                ("name", "=", f"Load #{lead.id}"),
            ],
            limit=1,
        )
        vals = {
            "name": f"Load #{lead.id}",
            "start": lead.departure_time,
            "stop": last_eta,
            "partner_ids": [(6, 0, [driver_partner.id])],
            "res_model": "crm.lead",
            "res_id": lead.id,
        }
        if existing:
            existing.write(vals)
        else:
            self.env["calendar.event"].create(vals)

    def _apply_routes(self, lead):
        warnings = []
        ordered_stops = lead.dispatch_stop_ids.sorted("sequence")
        if not ordered_stops:
            return warnings

        origin = lead.assigned_vehicle_id.home_location if lead.assigned_vehicle_id else None
        segments = self.mapbox_service.calculate_trip_segments(ordered_stops, origin_address=origin)

        leave_yard_at, total_distance, total_hours = self._compute_stop_schedule(ordered_stops, segments)

        lead.write({"departure_time": leave_yard_at, "total_distance_km": total_distance, "total_drive_hours": total_hours})
        self._create_calendar_booking(lead)

        for segment in segments:
            if segment.get("warning"):
                warnings.append(segment["warning"])
        return warnings

    def _build_quote_email(self, lead, pricing_result, extraction):
        notes = extraction.get("notes") or "Standard line-haul quote."
        return (
            f"Hello {lead.partner_name or 'Team'},<br/><br/>"
            "Thank you for the freight opportunity. Please find our proposed rate below:<br/><br/>"
            f"• Distance: <b>{lead.total_distance_km:.1f} km</b><br/>"
            f"• Estimated drive time: <b>{lead.total_drive_hours:.2f} hrs</b><br/>"
            f"• Estimated operating cost: <b>${pricing_result['estimated_cost']:.0f}</b><br/>"
            f"• Suggested all-in linehaul rate: <b>${pricing_result['suggested_rate']:.0f}</b><br/><br/>"
            f"Notes: {notes}<br/>"
            "Quote validity: 24 hours, subject to appointment timing and accessorial confirmation "
            "(detention, lumper, or special handling if applicable).<br/><br/>"
            "If approved, please reply with a rate confirmation and we will secure capacity immediately.<br/><br/>"
            "Best regards,<br/>"
            "PremaFirm Logistics Dispatch"
        )

    def _extract_po_details(self, email_text):
        text = email_text or ""
        po_match = re.search(r"(?:PO|Purchase\s*Order)\s*[:#-]?\s*([A-Z0-9-]+)", text, re.I)
        terms_match = re.search(r"(?:payment\s*terms?)\s*[:#-]?\s*([A-Za-z0-9\s-]+)", text, re.I)
        return {
            "po_number": po_match.group(1) if po_match else None,
            "payment_terms_text": terms_match.group(1).strip() if terms_match else None,
        }

    def process_lead(self, lead, email_text):
        extraction = self.ai_service.extract_load(email_text)
        po_data = self._extract_po_details(email_text)
        stop_vals = self._normalize_stop_values(
            extraction.get("stops", []),
            total_weight_lbs=extraction.get("total_weight_lbs"),
            total_pallets=extraction.get("total_pallets"),
            default_service_type="dry",
        )

        warnings = list(extraction.get("warnings") or [])
        if not stop_vals:
            warnings.append("No dispatch stops could be extracted from email content.")
            lead.write({"ai_recommendation": "\n".join(warnings)})
            return {"warnings": warnings, "pricing": {"estimated_cost": 0.0, "suggested_rate": 0.0}}

        lead.dispatch_stop_ids.unlink()
        for vals in stop_vals:
            vals["lead_id"] = lead.id
            self.env["premafirm.dispatch.stop"].create(vals)

        updates = {
            "inside_delivery": bool(extraction.get("inside_delivery")),
            "liftgate": bool(extraction.get("liftgate")),
            "detention_requested": bool(extraction.get("detention_requested")),
        }
        if po_data.get("po_number"):
            updates["po_number"] = po_data["po_number"]
        lead.write(updates)

        warnings.extend(self._apply_routes(lead))

        pricing_result = self.pricing_engine.calculate_pricing(lead)
        warnings.extend(pricing_result.get("warnings", []))

        recommendation_parts = [pricing_result["recommendation"]]
        if extraction.get("cross_border"):
            recommendation_parts.append("Cross-border shipment detected; verify customs paperwork and transit buffer.")
        if warnings:
            recommendation_parts.append("Warnings: " + " | ".join(dict.fromkeys(warnings)))

        lead.write(
            {
                "estimated_cost": pricing_result["estimated_cost"],
                "suggested_rate": pricing_result["suggested_rate"],
                "ai_recommendation": " ".join(recommendation_parts),
            }
        )

        quote_body = self._build_quote_email(lead, pricing_result, extraction)
        try:
            lead.message_post(body=quote_body)
        except Exception:
            _logger.exception("Could not post quote message for lead %s", lead.id)

        return {"warnings": warnings, "pricing": pricing_result}
