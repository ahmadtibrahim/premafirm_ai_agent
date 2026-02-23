
from datetime import timedelta

from odoo import fields

from .mapbox_service import MapboxService

DEADHEAD_WEIGHT_PER_KM = 0.35


class RunPlannerService:
    def __init__(self, env):
        self.env = env
        self.map_service = MapboxService(env)

    def get_or_create_run(self, vehicle_id, run_date):
        run = self.env["premafirm.dispatch.run"].search(
            [("vehicle_id", "=", vehicle_id.id), ("run_date", "=", run_date)], limit=1
        )
        if run:
            return run
        return self.env["premafirm.dispatch.run"].create(
            {
                "name": f"Run {vehicle_id.display_name} - {run_date}",
                "vehicle_id": vehicle_id.id,
                "run_date": run_date,
            }
        )

    def append_lead_to_run(self, run, lead):
        ordered = run.stop_ids.sorted("run_sequence")
        next_seq = (ordered[-1].run_sequence + 1) if ordered else 1
        for stop in lead.dispatch_stop_ids.sorted("sequence"):
            stop.write({"run_id": run.id, "run_sequence": next_seq})
            next_seq += 1
        lead.dispatch_run_id = run.id

    def simulate_run(self, run, stops):
        ordered = list(stops)
        home = run.vehicle_id.home_location if run.vehicle_id else None
        try:
            segments = self.map_service.calculate_trip_segments(home, ordered, return_home=True)
        except TypeError:
            segments = self.map_service.calculate_trip_segments(ordered, origin_address=home)

        cargo_count = 0
        empty_km = 0.0
        loaded_km = 0.0
        total_km = 0.0
        total_hours = 0.0
        etas = []
        now = fields.Datetime.now()
        for idx, stop in enumerate(ordered):
            seg = segments[idx] if idx < len(segments) else {}
            seg_km = float(seg.get("distance_km") or 0.0)
            seg_hr = float(seg.get("drive_hours") or 0.0)
            if cargo_count > 0:
                loaded_km += seg_km
            else:
                empty_km += seg_km
            total_km += seg_km
            total_hours += seg_hr
            now += timedelta(hours=seg_hr, minutes=stop.stop_service_mins or 0)
            etas.append(now)
            cargo_count += stop.cargo_delta

        return {
            "feasible": True,
            "segments": segments,
            "etas": etas,
            "total_distance_km": total_km,
            "total_drive_hours": total_hours,
            "empty_distance_km": empty_km,
            "loaded_distance_km": loaded_km,
        }

    def _score_option(self, base_sim, option_sim, lead):
        old_empty = float(base_sim.get("empty_distance_km") or 0.0)
        new_empty = float(option_sim.get("empty_distance_km") or 0.0)
        deadhead_reduction = old_empty - new_empty
        incremental_cost = max((option_sim.get("total_distance_km", 0.0) - base_sim.get("total_distance_km", 0.0)) * 1.65, 0.0)
        revenue = lead.final_rate or lead.suggested_rate or 0.0
        incremental_profit = revenue - incremental_cost
        score = incremental_profit + deadhead_reduction * DEADHEAD_WEIGHT_PER_KM
        return score, incremental_profit, deadhead_reduction

    def optimize_insertion_for_lead(self, lead):
        if not lead.assigned_vehicle_id:
            return {"feasible": False, "text": "No assigned vehicle. Assign a vehicle before optimization.", "options": []}
        run_date = fields.Date.to_date((lead.leave_yard_at or fields.Datetime.now()).date())
        run = self.get_or_create_run(lead.assigned_vehicle_id, run_date)
        base_stops = run.stop_ids.sorted("run_sequence")
        if not base_stops:
            self.append_lead_to_run(run, lead)
            sim = self.simulate_run(run, run.stop_ids.sorted("run_sequence"))
            self._update_run(run, sim)
            return {"feasible": True, "text": "Created new run and appended lead as first route.", "options": []}

        new_stops = lead.dispatch_stop_ids.sorted("sequence")
        if len(new_stops) < 2:
            return {"feasible": False, "text": "Lead needs at least pickup and delivery stops for insertion.", "options": []}

        base_sim = self.simulate_run(run, base_stops)
        options = []
        n = len(base_stops)
        pu = new_stops[0]
        dl = new_stops[1]
        for i in range(0, n + 1):
            for j in range(i + 1, n + 2):
                candidate = list(base_stops)
                candidate.insert(i, pu)
                candidate.insert(j, dl)
                sim = self.simulate_run(run, candidate)
                score, inc_profit, dh = self._score_option(base_sim, sim, lead)
                options.append(
                    {
                        "pickup_idx": i,
                        "delivery_idx": j,
                        "score": score,
                        "incremental_profit": inc_profit,
                        "deadhead_reduction": dh,
                        "added_km": sim["total_distance_km"] - base_sim["total_distance_km"],
                        "added_hours": sim["total_drive_hours"] - base_sim["total_drive_hours"],
                        "simulation": sim,
                        "order": candidate,
                    }
                )
        options = sorted(options, key=lambda o: o["score"], reverse=True)[:3]
        text_lines = ["Top AI schedule options:"]
        for idx, option in enumerate(options, 1):
            text_lines.append(
                f"Option {idx}: +{option['added_km']:.1f} km, +{option['added_hours']:.2f} h, "
                f"deadhead Δ {option['deadhead_reduction']:.1f} km, incremental profit ${option['incremental_profit']:.2f}."
            )
        return {"feasible": bool(options), "text": "\n".join(text_lines), "options": options, "run_id": run.id}

    def apply_option(self, lead, option):
        run = self.env["premafirm.dispatch.run"].browse(option.get("run_id"))
        ordered = option.get("order") or []
        for seq, stop in enumerate(ordered, 1):
            stop.write({"run_id": run.id, "run_sequence": seq})
            stop.lead_id.dispatch_run_id = run.id
        sim = option.get("simulation") or self.simulate_run(run, run.stop_ids.sorted("run_sequence"))
        self._update_run(run, sim)

    def _get_driver_partner(self, vehicle):
        # Fleet standard: vehicle.driver_id is already a res.partner record.
        if not vehicle:
            return False
        return vehicle.driver_id or False

    def _update_run(self, run, simulation):
        start = fields.Datetime.now()
        end = start + timedelta(hours=float(simulation.get("total_drive_hours") or 0.0))
        run_vals = {
            "start_datetime": start,
            "end_datetime": end,
            "total_distance_km": simulation.get("total_distance_km", 0.0),
            "total_drive_hours": simulation.get("total_drive_hours", 0.0),
            "empty_distance_km": simulation.get("empty_distance_km", 0.0),
            "loaded_distance_km": simulation.get("loaded_distance_km", 0.0),
            "estimated_profit": (run.estimated_revenue or 0.0) - (run.estimated_cost or 0.0),
        }
        run.write(run_vals)


        driver_partner = self._get_driver_partner(run.vehicle_id)
        notify_driver = any(bool(stop.lead_id.notify_driver) for stop in run.stop_ids if stop.lead_id)
        partner_ids = [driver_partner.id] if (notify_driver and driver_partner) else []
        vals = {
            "name": run.name,
            "start": run.start_datetime,
            "stop": run.end_datetime,
            "partner_ids": [(6, 0, partner_ids)],
            "res_model": "premafirm.dispatch.run",
            "res_id": run.id,
        }
        if "vehicle_id" in self.env["calendar.event"]._fields:
            vals["vehicle_id"] = run.vehicle_id.id
        if run.calendar_event_id:
            run.calendar_event_id.with_context(mail_notify_force_send=False, mail_auto_subscribe_no_notify=True).write(vals)
        else:
            run.calendar_event_id = self.env["calendar.event"].with_context(mail_notify_force_send=False, mail_auto_subscribe_no_notify=True).create(vals)
