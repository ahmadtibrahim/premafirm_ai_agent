import logging
import math
from datetime import datetime, time, timedelta

from odoo import fields

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
            scheduled_datetime = stop.get("scheduled_datetime")
            if stop_type == "pickup" and not stop.get("window_start"):
                scheduled_datetime = datetime.combine(fields.Date.today(), time(9, 0))
            stops.append(
                {
                    "sequence": seq,
                    "stop_type": stop_type,
                    "address": address,
                    "pallets": stop.get("pallets"),
                    "weight_lbs": stop.get("weight_lbs"),
                    "service_type": stop.get("service_type") or default_service_type,
                    "pickup_window_start": stop.get("window_start") if stop_type == "pickup" else None,
                    "pickup_window_end": stop.get("window_end") if stop_type == "pickup" else None,
                    "delivery_window_start": stop.get("window_start") if stop_type == "delivery" else None,
                    "delivery_window_end": stop.get("window_end") if stop_type == "delivery" else None,
                    "scheduled_datetime": scheduled_datetime,
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

    def _apply_routes(self, lead):
        warnings = []
        ordered_stops = lead.dispatch_stop_ids.sorted("sequence")
        total_distance = 0.0
        total_hours = 0.0
        if not ordered_stops:
            return warnings

        vehicle = lead.assigned_vehicle_id
        depot = (vehicle.home_location if vehicle and vehicle.home_location else "5585 McAdam Rd, Mississauga, ON L4Z 1P1")

        first_route = self.mapbox_service.get_route(depot, ordered_stops[0].address)
        ordered_stops[0].write(
            {
                "distance_km": first_route.get("distance_km", 0.0),
                "drive_hours": first_route.get("drive_hours", 0.0),
            }
        )
        total_distance += float(first_route.get("distance_km", 0.0))
        total_hours += float(first_route.get("drive_hours", 0.0))
        if first_route.get("warning"):
            warnings.append(first_route["warning"])

        for i in range(len(ordered_stops) - 1):
            route = self.mapbox_service.get_route(ordered_stops[i].address, ordered_stops[i + 1].address)
            ordered_stops[i + 1].write(
                {
                    "distance_km": route.get("distance_km", 0.0),
                    "drive_hours": route.get("drive_hours", 0.0),
                }
            )
            total_distance += float(route.get("distance_km", 0.0))
            total_hours += float(route.get("drive_hours", 0.0))
            if route.get("warning"):
                warnings.append(route["warning"])

        final_route = self.mapbox_service.get_route(ordered_stops[-1].address, depot)
        total_distance += float(final_route.get("distance_km", 0.0))
        total_hours += float(final_route.get("drive_hours", 0.0))
        if final_route.get("warning"):
            warnings.append(final_route["warning"])

        lead.write({"total_distance_km": total_distance, "total_drive_hours": total_hours})

        pickups = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")
        if pickups:
            first = pickups[0]
            if first.scheduled_datetime and first.drive_hours:
                lead.departure_time = first.scheduled_datetime - timedelta(hours=first.drive_hours)

        for stop in lead.dispatch_stop_ids:
            if stop.scheduled_datetime and stop.drive_hours:
                stop.estimated_arrival = stop.scheduled_datetime + timedelta(hours=stop.drive_hours)

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

    def process_lead(self, lead, email_text):
        extraction = self.ai_service.extract_load(email_text)
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

        lead.write(
            {
                "inside_delivery": bool(extraction.get("inside_delivery")),
                "liftgate": bool(extraction.get("liftgate")),
                "detention_requested": bool(extraction.get("detention_requested")),
            }
        )

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
