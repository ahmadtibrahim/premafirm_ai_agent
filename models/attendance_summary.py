"""
Daily Staff Activity Summary — extended with full Odoo activity metrics.

Tracks: hours worked, active vs idle time, messages, emails, VoIP calls,
notes, file uploads, activities — all sourced from Odoo's own data.
Every action Grace or any employee takes inside Odoo is captured here.
"""
import json
import logging
from datetime import datetime, time, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

IDLE_GAP_MINUTES = 20       # gap between actions before we call it "idle"
SUSPICIOUS_IDLE_MINUTES = 45  # gap this long is flagged as "extended idle"


def _round_hours(minutes):
    return round(minutes / 60, 2)


class PremaAttendanceSummary(models.Model):
    _name = "prema.attendance.summary"
    _description = "Daily Staff Activity Summary"
    _order = "date desc, employee_id"

    # ── Identity ──────────────────────────────────────────────────
    employee_id = fields.Many2one(
        "hr.employee", string="Employee", required=True, ondelete="cascade", index=True
    )
    date = fields.Date(string="Date", required=True, default=fields.Date.today, index=True)

    # ── Attendance ─────────────────────────────────────────────────
    check_in = fields.Datetime(string="Check-in")
    check_out = fields.Datetime(string="Check-out")
    worked_hours = fields.Float(string="Logged Hours", digits=(5, 2),
        help="Total time from first check-in to last check-out (raw attendance).")
    active_hours = fields.Float(string="Active Hours", digits=(5, 2),
        help="Worked hours minus idle gaps. Time actually doing things in Odoo.")
    idle_hours = fields.Float(string="Idle Hours", digits=(5, 2),
        help="Time logged in but with no Odoo actions for more than 20 minutes.")
    idle_periods_text = fields.Text(string="Idle Periods",
        help="List of idle gaps with start, end, and duration.")
    first_action = fields.Datetime(string="First Action")
    last_action = fields.Datetime(string="Last Action")

    # ── Activity Counts ────────────────────────────────────────────
    messages_count = fields.Integer(string="Messages / Notes")
    emails_count = fields.Integer(string="Emails Sent")
    calls_outgoing = fields.Integer(string="Outgoing Calls")
    calls_incoming = fields.Integer(string="Incoming Calls")
    call_duration_minutes = fields.Float(string="Call Time (min)", digits=(6, 1))
    activities_count = fields.Integer(string="Activities Done")
    files_uploaded = fields.Integer(string="Files Uploaded")

    # ── AI Output ─────────────────────────────────────────────────
    ai_summary = fields.Text(string="AI Summary")
    contacts_met = fields.Text(string="Activity Detail Log")
    ai_advice = fields.Text(string="AI Advice")

    state = fields.Selection([
        ("pending",  "Pending"),
        ("done",     "Generated"),
        ("no_data",  "No Attendance"),
    ], default="pending", string="Status")

    _sql_constraints = [
        ("unique_employee_date", "unique(employee_id, date)",
         "Summary already exists for this employee and date."),
    ]

    # ── Cron: main daily runner ────────────────────────────────────

    @api.model
    def _run_daily_summary(self):
        """Called nightly at 6PM EST. Generate summaries then send coaching messages."""
        today = fields.Date.today()
        employees = self.env["hr.employee"].search([("user_id", "!=", False), ("active", "=", True)])
        for employee in employees:
            try:
                self._generate_summary(employee, today)
            except Exception as exc:
                _logger.error("Attendance summary failed for %s: %s", employee.name, exc)

        # After summaries are generated, send coaching messages
        try:
            self.env["prema.staff.coaching.config"]._run_daily_coaching()
        except Exception as exc:
            _logger.error("Daily coaching dispatch failed: %s", exc)

    def action_regenerate(self):
        """Button: manually regenerate this record."""
        self.ensure_one()
        self._generate_summary(self.employee_id, self.date)

    # ── Core generator ─────────────────────────────────────────────

    @api.model
    def _generate_summary(self, employee, date):
        existing = self.search([
            ("employee_id", "=", employee.id), ("date", "=", date)
        ])
        record = existing or self.create({"employee_id": employee.id, "date": date})

        user = employee.user_id
        partner = user.partner_id if user else None

        day_start = datetime.combine(date, time.min)
        day_end = datetime.combine(date, time.max)

        # ── Attendance ─────────────────────────────────────────────
        attendances = self.env["hr.attendance"].search([
            ("employee_id", "=", employee.id),
            ("check_in", ">=", day_start),
            ("check_in", "<=", day_end),
        ], order="check_in")

        if not attendances:
            record.write({"state": "no_data"})
            return

        first_in = attendances[0].check_in
        last_out = attendances[-1].check_out or fields.Datetime.now()
        worked_secs = sum(
            (a.check_out or fields.Datetime.now() - a.check_in).total_seconds()
            if isinstance(a.check_out or True, bool)
            else (a.check_out - a.check_in).total_seconds()
            for a in attendances
        )
        # Simpler:
        worked_secs = 0
        for a in attendances:
            cout = a.check_out or datetime.utcnow()
            if cout > a.check_in:
                worked_secs += (cout - a.check_in).total_seconds()
        worked_hours = round(worked_secs / 3600, 2)

        # ── Gather all action timestamps ──────────────────────────
        all_timestamps = []

        # Messages (comments, emails, call logs)
        msgs_comment = 0
        msgs_email = 0
        if partner:
            msgs = self.env["mail.message"].search([
                ("author_id", "=", partner.id),
                ("date", ">=", day_start),
                ("date", "<=", day_end),
                ("message_type", "in", ("email", "comment", "auto_comment")),
            ])
            for m in msgs:
                all_timestamps.append(m.date)
                if m.message_type == "email":
                    msgs_email += 1
                else:
                    msgs_comment += 1

        # VoIP calls
        calls_out = calls_in = 0
        call_duration_min = 0.0
        call_log_lines = []
        if user:
            calls = self.env["voip.call"].search([
                ("user_id", "=", user.id),
                ("start_date", ">=", day_start),
                ("start_date", "<=", day_end),
                ("state", "=", "terminated"),
            ])
            for c in calls:
                if c.start_date:
                    all_timestamps.append(c.start_date)
                if c.direction == "outgoing":
                    calls_out += 1
                else:
                    calls_in += 1
                if c.start_date and c.end_date:
                    duration_sec = (c.end_date - c.start_date).total_seconds()
                    call_duration_min += duration_sec / 60
                    partner_name = c.partner_id.name if c.partner_id else c.phone_number
                    call_log_lines.append(
                        f"  {'↑' if c.direction == 'outgoing' else '↓'} "
                        f"{c.start_date.strftime('%H:%M')} {partner_name} "
                        f"({duration_sec/60:.1f}m)"
                    )

        # Activities completed
        acts_done = 0
        if user:
            # mail.activity with date_deadline == today (completed activities show here)
            acts = self.env["mail.activity"].search([
                ("user_id", "=", user.id),
                ("write_date", ">=", day_start),
                ("write_date", "<=", day_end),
            ])
            # Also search mail.message for activity feedback (logged when done)
            if partner:
                done_msgs = self.env["mail.message"].search([
                    ("author_id", "=", partner.id),
                    ("message_type", "=", "auto_comment"),
                    ("date", ">=", day_start),
                    ("date", "<=", day_end),
                ])
                acts_done = len(done_msgs)
                for m in done_msgs:
                    all_timestamps.append(m.date)

        # Files uploaded
        files_up = 0
        if user:
            attachments = self.env["ir.attachment"].search([
                ("create_uid", "=", user.id),
                ("create_date", ">=", day_start),
                ("create_date", "<=", day_end),
            ])
            files_up = len(attachments)
            for a in attachments:
                all_timestamps.append(a.create_date)

        # ── Compute idle periods ──────────────────────────────────
        all_timestamps = sorted(set(t for t in all_timestamps if t))
        first_action = all_timestamps[0] if all_timestamps else None
        last_action = all_timestamps[-1] if all_timestamps else None

        idle_gaps = []
        idle_total_min = 0.0
        suspicious_gaps = []

        # Use attendance boundaries as outer timestamps
        boundary_start = first_in
        boundary_end = last_out

        if all_timestamps:
            # Check gap from check-in to first action
            gap_before = (all_timestamps[0] - boundary_start).total_seconds() / 60
            if gap_before > IDLE_GAP_MINUTES:
                idle_gaps.append((boundary_start, all_timestamps[0], gap_before))
                idle_total_min += gap_before
                if gap_before > SUSPICIOUS_IDLE_MINUTES:
                    suspicious_gaps.append((boundary_start, all_timestamps[0], gap_before))

            # Check gaps between consecutive actions
            for i in range(len(all_timestamps) - 1):
                gap_min = (all_timestamps[i + 1] - all_timestamps[i]).total_seconds() / 60
                if gap_min > IDLE_GAP_MINUTES:
                    idle_gaps.append((all_timestamps[i], all_timestamps[i + 1], gap_min))
                    idle_total_min += gap_min
                    if gap_min > SUSPICIOUS_IDLE_MINUTES:
                        suspicious_gaps.append((all_timestamps[i], all_timestamps[i + 1], gap_min))

            # Check gap from last action to check-out
            gap_after = (boundary_end - all_timestamps[-1]).total_seconds() / 60
            if gap_after > IDLE_GAP_MINUTES:
                idle_gaps.append((all_timestamps[-1], boundary_end, gap_after))
                idle_total_min += gap_after
                if gap_after > SUSPICIOUS_IDLE_MINUTES:
                    suspicious_gaps.append((all_timestamps[-1], boundary_end, gap_after))
        else:
            # No actions at all during attendance — entire shift is idle
            idle_total_min = worked_secs / 60
            suspicious_gaps.append((first_in, last_out, idle_total_min))

        active_hours = max(0, round((worked_secs / 60 - idle_total_min) / 60, 2))
        idle_hours = round(idle_total_min / 60, 2)

        # ── Format idle periods text ──────────────────────────────
        idle_lines = []
        for (start, end, gap_min) in idle_gaps:
            flag = " ⚠️" if gap_min >= SUSPICIOUS_IDLE_MINUTES else ""
            idle_lines.append(
                f"  {start.strftime('%H:%M')} → {end.strftime('%H:%M')}"
                f" ({gap_min:.0f} min){flag}"
            )
        idle_periods_text = "\n".join(idle_lines) if idle_lines else "None — continuous activity"

        # ── Build activity detail log ─────────────────────────────
        detail_lines = []

        # Check-in/out block
        detail_lines.append(
            f"CHECK-IN:  {first_in.strftime('%I:%M %p')} UTC\n"
            f"CHECK-OUT: {(last_out.strftime('%I:%M %p') + ' UTC') if attendances[-1].check_out else '(still open)'}\n"
            f"LOGGED: {worked_hours:.2f}h  |  ACTIVE: {active_hours:.2f}h  |  IDLE: {idle_hours:.2f}h"
        )

        # Call log
        if call_log_lines:
            detail_lines.append(
                f"\nVOIP CALLS ({calls_out} out, {calls_in} in, {call_duration_min:.1f} min total):"
            )
            detail_lines.extend(call_log_lines[:30])

        # Messages block
        if msgs_comment or msgs_email:
            detail_lines.append(
                f"\nMESSAGES: {msgs_comment} notes/chatter  |  {msgs_email} emails sent"
            )

        # Idle / suspicious blocks
        if suspicious_gaps:
            detail_lines.append(f"\n⚠️  EXTENDED IDLE PERIODS ({len(suspicious_gaps)}):")
            for (start, end, gap_min) in suspicious_gaps:
                detail_lines.append(
                    f"  {start.strftime('%H:%M')} → {end.strftime('%H:%M')} "
                    f"({gap_min:.0f} min of no Odoo activity)"
                )

        # Hour-by-hour summary
        if all_timestamps:
            hourly = {}
            for ts in all_timestamps:
                h = ts.hour
                hourly[h] = hourly.get(h, 0) + 1
            hour_summary = "  ".join(
                f"{h:02d}:00 ({n})" for h, n in sorted(hourly.items())
            )
            detail_lines.append(f"\nACTIVITY BY HOUR:\n  {hour_summary}")

        activity_detail = "\n".join(detail_lines)

        # ── GPT summary ───────────────────────────────────────────
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import get_api_key as _get_deepseek_key
        api_key = _get_deepseek_key(self.env)

        ai_summary = ""
        ai_advice = ""

        total_actions = msgs_comment + msgs_email + calls_out + calls_in + acts_done + files_up

        if api_key and total_actions > 0:
            from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat

            prompt = (
                f"Employee: {employee.name}\n"
                f"Date: {date}\n\n"
                f"ATTENDANCE:\n"
                f"  Logged: {worked_hours:.2f}h | Active: {active_hours:.2f}h | Idle: {idle_hours:.2f}h\n"
                f"  Check-in: {first_in.strftime('%H:%M UTC')} | "
                f"Check-out: {last_out.strftime('%H:%M UTC') if attendances[-1].check_out else 'open'}\n\n"
                f"ACTIVITY:\n"
                f"  Outgoing calls: {calls_out} ({call_duration_min:.0f} min)\n"
                f"  Incoming calls: {calls_in}\n"
                f"  Messages/notes: {msgs_comment}\n"
                f"  Emails sent: {msgs_email}\n"
                f"  Activities done: {acts_done}\n"
                f"  Files uploaded: {files_up}\n\n"
            )
            if suspicious_gaps:
                prompt += (
                    f"IDLE PERIODS (>45 min with no Odoo activity):\n"
                    + "\n".join(
                        f"  {s.strftime('%H:%M')}–{e.strftime('%H:%M')} ({g:.0f} min)"
                        for s, e, g in suspicious_gaps
                    ) + "\n\n"
                )
            prompt += (
                "Write:\n"
                f"1. 3-4 sentence summary of what {employee.name} did today.\n"
                "2. Two specific actionable pieces of advice for tomorrow.\n\n"
                "Format:\nSUMMARY:\n<text>\n\nADVICE:\n<text>"
            )
            try:
                response = deepseek_chat(
                    messages=[{"role": "user", "content": prompt}],
                    system=(
                        "You are Ahmad Ibrahim's AI assistant at PremaFirm Inc. "
                        "Generate concise, honest daily work summaries. "
                        "If idle periods exist, mention them factually without judgment. "
                        "Be specific with advice."
                    ),
                    max_tokens=500,
                    api_key=api_key,
                )
                if "ADVICE:" in response:
                    parts = response.split("ADVICE:", 1)
                    ai_summary = parts[0].replace("SUMMARY:", "").strip()
                    ai_advice = parts[1].strip()
                else:
                    ai_summary = response.strip()
            except Exception as exc:
                _logger.error("GPT summary error for %s: %s", employee.name, exc)
                ai_summary = f"AI call failed: {exc}"
        elif total_actions == 0:
            ai_summary = (
                f"No Odoo activity recorded on {date}. "
                f"Employee was logged in for {worked_hours:.1f}h "
                f"(check-in: {first_in.strftime('%H:%M')}) but no messages, calls, emails, "
                f"or file uploads were found."
            )
        else:
            ai_summary = (
                f"Summary for {date}: {worked_hours:.1f}h logged, {active_hours:.1f}h active. "
                f"{calls_out} outgoing calls ({call_duration_min:.0f} min), "
                f"{msgs_comment + msgs_email} messages/emails."
            )

        # ── Write record ──────────────────────────────────────────
        record.write({
            "check_in": first_in,
            "check_out": last_out if attendances[-1].check_out else False,
            "worked_hours": worked_hours,
            "active_hours": active_hours,
            "idle_hours": idle_hours,
            "idle_periods_text": idle_periods_text,
            "first_action": first_action,
            "last_action": last_action,
            "messages_count": msgs_comment,
            "emails_count": msgs_email,
            "calls_outgoing": calls_out,
            "calls_incoming": calls_in,
            "call_duration_minutes": round(call_duration_min, 1),
            "activities_count": acts_done,
            "files_uploaded": files_up,
            "contacts_met": activity_detail,
            "ai_summary": ai_summary,
            "ai_advice": ai_advice,
            "state": "done",
        })
