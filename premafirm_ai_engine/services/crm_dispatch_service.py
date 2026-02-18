import logging
import re
from datetime import datetime, time, timedelta

import pytz
from odoo import fields
from odoo.exceptions import UserError

from .ai_extraction_service import AIExtractionService
from .mapbox_service import MapboxService
from .pricing_engine import PricingEngine

_logger = logging.getLogger(__name__)


def _normalize_odoo_datetime(value):
    if not value:
        return False
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", ""))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
    return False


class CRMDispatchService:
    def __init__(self, env):
        self.env = env
        self.ai_service = AIExtractionService(env)
        self.mapbox_service = MapboxService(env)
        self.pricing_engine = PricingEngine(env)

    def _now_toronto(self):
        tz = pytz.timezone("America/Toronto")
        return datetime.now(tz)

    def _validate_numeric_fields(self, stops):
        errors = []
        for idx, stop in enumerate(stops, 1):
            pallets = stop.get("pallets")
            weight = stop.get("weight_lbs")
            if pallets is None or str(pallets).strip() == "":
                errors.append(f"Stop {idx}: missing pallet count.")
            elif not isinstance(pallets, (int, float)):
                errors.append(f"Stop {idx}: pallet count must be numeric.")
            if weight is None or str(weight).strip() == "":
                errors.append(f"Stop {idx}: missing weight.")
            elif not isinstance(weight, (int, float)):
                errors.append(f"Stop {idx}: weight must be numeric.")
        return errors

    def _normalize_stop_values(self, extracted_stops):
        stops = []
        for seq, stop in enumerate(extracted_stops or [], 1):
            stop_type = stop.get("stop_type") if stop.get("stop_type") in ("pickup", "delivery") else None
            address = (stop.get("address") or "").strip()
            if not stop_type or not address:
                continue
            requested = stop.get("scheduled_datetime")
            if stop_type == "pickup" and not requested:
                requested = datetime.combine(self._now_toronto().date(), time(9, 0)).isoformat()
            stops.append(
                {
                    "sequence": int(stop.get("sequence") or seq),
                    "stop_type": stop_type,
                    "address": address,
                    "country": stop.get("country") or "",
                    "pallets": int(float(stop.get("pallets") or 0)),
                    "weight_lbs": float(stop.get("weight_lbs") or 0.0),
                    "service_type": "reefer" if stop.get("service_type") == "reefer" else "dry",
                    "scheduled_datetime": requested,
                    "special_instructions": stop.get("special_instructions"),
                }
            )
        return sorted(stops, key=lambda s: (s["sequence"], 0 if s["stop_type"] == "pickup" else 1))

    def _compute_break_hours(self, segment_drive_hours, state):
        break_hours = 0.0
        before = state.get("since_major", 0.0)
        after = before + segment_drive_hours
        if before < 4 <= after:
            break_hours += 0.25
        if before < 8 <= after:
            break_hours += 0.75
            state["since_major"] = max(after - 8, 0.0)
        else:
            state["since_major"] = after
        if state["since_major"] >= 3.0:
            break_hours += 0.1667
            state["since_major"] = 0.0
        return break_hours

    def _infer_liftgate(self, categories):
        cats = {c.lower() for c in (categories or [])}
        if cats.intersection({"restaurant", "cafe", "convenience_store", "residential"}):
            return True
        if cats.intersection({"supermarket", "distribution_center", "warehouse", "big_box_store"}):
            return False
        return False

    def _enrich_stop_geodata(self, stop_vals):
        warnings = []
        for stop in stop_vals:
            geo = self.mapbox_service.geocode_address(stop["address"])
            if geo.get("warning") or not geo.get("latitude") or not geo.get("longitude"):
                stop["full_address"] = stop["address"]
                stop["needs_manual_review"] = True
                warnings.append(f"Geocoding failed for stop '{stop['address']}', manual review required.")
                continue
            stop["full_address"] = geo.get("full_address")
            stop["address"] = geo.get("short_address") or stop["address"]
            stop["postal_code"] = geo.get("postal_code")
            stop["latitude"] = geo.get("latitude")
            stop["longitude"] = geo.get("longitude")
            stop["place_categories"] = ",".join(geo.get("place_categories") or [])
            stop["liftgate_needed"] = self._infer_liftgate(geo.get("place_categories"))
        return warnings

    def _compute_stop_schedule(self, ordered_stops, segments):
        first_pickup = ordered_stops.filtered(lambda s: s.stop_type == "pickup")[:1]
        if first_pickup and not first_pickup.scheduled_datetime:
            first_pickup.scheduled_datetime = datetime.combine(self._now_toronto().date(), time(9, 0))

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
                stop.scheduled_datetime = running_dt + timedelta(hours=effective_drive_hours)
            estimated_arrival = running_dt + timedelta(hours=effective_drive_hours)
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
        overlap = self.env["calendar.event"].search(
            [("partner_ids", "in", driver_partner.id), ("start", "<", last_eta), ("stop", ">", lead.departure_time)],
            limit=1,
        )
        if overlap:
            raise UserError("Driver already has an overlapping booking in Calendar.")
        existing = self.env["calendar.event"].search(
            [("res_model", "=", "crm.lead"), ("res_id", "=", lead.id), ("name", "=", f"Load #{lead.id}")],
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

    def _extract_po_details(self, email_text):
        text = email_text or ""
        po_match = re.search(r"(?:PO|Purchase\s*Order)\s*[:#-]?\s*([A-Z0-9-]+)", text, re.I)
        return {"po_number": po_match.group(1) if po_match else None}

    def _determine_freight_service(self, lead, extraction):
        pickups = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "pickup")
        deliveries = lead.dispatch_stop_ids.filtered(lambda s: s.stop_type == "delivery")
        is_ltl = len(pickups) > 1 or len(deliveries) > 1 or len(lead.dispatch_stop_ids) > 2
        country_us = any("US" in (s.country or "").upper() or "USA" in (s.country or "").upper() for s in lead.dispatch_stop_ids)
        xmlid = "premafirm_ai_engine.product_ltl_usa" if country_us and is_ltl else (
            "premafirm_ai_engine.product_ftl_usa" if country_us else (
                "premafirm_ai_engine.product_ltl_can" if is_ltl else "premafirm_ai_engine.product_ftl_can"
            )
        )
        product_tmpl = self.env.ref(xmlid, raise_if_not_found=False)
        product = product_tmpl.product_variant_id if product_tmpl else False
        reefer_required = bool(extraction.get("reefer_required"))
        lead.write({"detention_requested": bool(extraction.get("detention_requested")), "reefer_required": reefer_required})
        for stop in lead.dispatch_stop_ids:
            stop.is_ftl = not is_ltl
            if product:
                stop.product_id = product.id
        return reefer_required

    def process_lead(self, lead, email_text, attachments=None):
        extraction = self.ai_service.extract_load(email_text, attachments=attachments)
        po_data = self._extract_po_details(email_text)
        stop_vals = self._normalize_stop_values(extraction.get("stops", []))
        warnings = list(extraction.get("warnings") or [])
        warnings.extend(extraction.get("errors") or [])

        validation_errors = self._validate_numeric_fields(stop_vals)
        warnings.extend(validation_errors)
        if validation_errors:
            lead.message_post(body="Missing or invalid stop values detected. Please clarify pallets/weight before auto rating.")
            lead.write({"ai_recommendation": " | ".join(dict.fromkeys(warnings))})
            return {"warnings": warnings, "pricing": {"estimated_cost": 0.0, "suggested_rate": 0.0}}

        if not stop_vals:
            warnings.append("No dispatch stops could be extracted from email content.")
            lead.write({"ai_recommendation": "\n".join(warnings)})
            return {"warnings": warnings, "pricing": {"estimated_cost": 0.0, "suggested_rate": 0.0}}

        warnings.extend(self._enrich_stop_geodata(stop_vals))

        lead.dispatch_stop_ids.unlink()
        for vals in stop_vals:
            vals["lead_id"] = lead.id
            vals["scheduled_datetime"] = _normalize_odoo_datetime(vals.get("scheduled_datetime"))
            self.env["premafirm.dispatch.stop"].create(vals)

        updates = {
            "inside_delivery": bool(extraction.get("inside_delivery")),
            "liftgate": bool(extraction.get("liftgate")),
            "detention_requested": bool(extraction.get("detention_requested")),
        }
        if po_data.get("po_number"):
            updates["po_number"] = po_data["po_number"]
        if extraction.get("source") == "attachment" and email_text:
            lead.message_post(body="Attachment and email body both contained data; attachment was prioritized.")
        lead.write(updates)

        reefer_required = self._determine_freight_service(lead, extraction)
        if reefer_required:
            lead.message_post(body="Reefer indicators found; reefer_required flagged for confirmation.")

        warnings.extend(self._apply_routes(lead))

        if validation_errors:
            pricing_result = {"estimated_cost": 0.0, "suggested_rate": 0.0, "warnings": warnings, "recommendation": "Incomplete data."}
        else:
            pricing_result = self.pricing_engine.calculate_pricing(lead)
            warnings.extend(pricing_result.get("warnings", []))

        recommendation = pricing_result.get("recommendation", "Dispatch processed.") + " Confirm with broker whether pump truck/dock-level equipment is required."
        if warnings:
            recommendation = f"{recommendation} Warnings: {' | '.join(dict.fromkeys(warnings))}"
        lead.write(
            {
                "estimated_cost": pricing_result.get("estimated_cost", 0.0),
                "suggested_rate": pricing_result.get("suggested_rate", 0.0),
                "ai_recommendation": recommendation,
            }
        )
        return {"warnings": warnings, "pricing": pricing_result}
