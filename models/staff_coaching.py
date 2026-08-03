"""
Staff Coaching & Daily/Weekly Notification System.

Daily  (6PM EST): post full team summary to private manager channel
                  + OdooBot DM to each employee with role-specific AI advice
Weekly (Mon 6AM): post team week-in-review to manager channel
                  + OdooBot DM to each employee with trend analysis vs prev weeks

AI always sees the last 4 weeks of data so it can identify progress or regression.
"""
import logging
from datetime import date, datetime, time, timedelta

import pytz
from markupsafe import Markup

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

TORONTO_TZ = pytz.timezone("America/Toronto")

IDLE_WARNING_PCT = 0.40  # flag row if idle > 40% of logged hours

ROLE_LABELS = {
    "cold_caller":  "Cold Caller & Appointment Setter",
    "hot_caller":   "Hot Lead Caller & Closer",
    "dispatcher":   "Dispatcher",
    "manager":      "Company Manager / Owner",
}

# ── Role-specific AI system prompts ───────────────────────────────────────────

ROLE_SYSTEM_PROMPTS = {
    "cold_caller": (
        "You are a sales coaching AI for PremaFirm Inc., a Canadian trucking and logistics company. "
        "You are coaching a Cold Caller & Appointment Setter. "
        "Her job is to make outbound calls, qualify prospects, and book discovery meetings. "
        "KPIs you care about: daily call volume (target: 40+), call duration (2-4 min = good), "
        "CRM notes logged per call, appointments booked per week, and time spent on Odoo. "
        "An idle gap over 45 min during work hours is a red flag. "
        "Be direct, specific, and encouraging. Reference the actual numbers. "
        "If metrics improved vs last week, acknowledge it. If they declined, address it honestly."
    ),
    "hot_caller": (
        "You are a sales coaching AI for PremaFirm Inc., a Canadian trucking and logistics company. "
        "You are coaching a Hot Lead Caller & Deal Closer. "
        "His job is to call warm/hot leads, set meetings, follow up, and close deals. "
        "KPIs you care about: calls to qualified leads (target: 10-15/day), emails sent, "
        "CRM pipeline stage movements, meetings booked, notes logged per interaction, "
        "and follow-up discipline. Low call volume is the biggest risk for a closer. "
        "Be direct, strategic, and results-focused. Reference actual numbers. "
        "Track progress vs previous weeks and call out improvements or declines."
    ),
    "dispatcher": (
        "You are an operations coaching AI for PremaFirm Inc., a Canadian trucking company. "
        "You are coaching a Dispatcher. "
        "Their job is to book loads, assign trucks, communicate with drivers, and manage exceptions. "
        "KPIs: dispatch jobs created/updated, stop completions, customer messages handled, "
        "response time to driver issues, and time logged in the dispatch app. "
        "Be practical and operationally focused. Reference actual Odoo activity."
    ),
    "manager": (
        "You are a business intelligence AI for PremaFirm Inc., a Canadian trucking company. "
        "You are advising the company owner/manager on overall team performance and company health. "
        "Look at the full team's data: call volume trends, CRM activity, idle time patterns, "
        "and week-over-week progress. Identify bottlenecks, coaching priorities, and process gaps. "
        "Be strategic and data-driven. Highlight what is working and what needs attention."
    ),
}

# ─────────────────────────────────────────────────────────────────────────────


class PremaStaffCoachingConfig(models.Model):
    _name = "prema.staff.coaching.config"
    _description = "Staff Coaching & Notification Configuration"
    _order = "employee_id"

    employee_id = fields.Many2one(
        "hr.employee", string="Employee", required=True, ondelete="cascade", index=True
    )
    role_type = fields.Selection([
        ("cold_caller", "Cold Caller & Appointment Setter"),
        ("hot_caller",  "Hot Lead Caller & Closer"),
        ("dispatcher",  "Dispatcher"),
        ("manager",     "Company Manager / Owner"),
    ], string="Role / Coaching Profile", required=True, default="cold_caller")

    @api.onchange("employee_id")
    def _onchange_employee_set_role(self):
        """Auto-fill role_type and job_responsibilities from the employee's job title."""
        if not self.employee_id:
            return
        title = (self.employee_id.job_title or "").lower().strip()
        if not title:
            return

        # Keyword → role mapping (first match wins)
        mapping = [
            (["cold call", "prospecting", "appointment setter", "lead gen", "outbound"], "cold_caller"),
            (["closer", "hot lead", "account exec", "account manager", "sales rep",
              "business dev", "bdr", "sdr"], "hot_caller"),
            (["dispatch", "logistics coord", "fleet coord", "operations coord"], "dispatcher"),
            (["manager", "owner", "director", "vp ", "president", "ceo", "coo",
              "admin", "supervisor", "head of"], "manager"),
        ]
        for keywords, role in mapping:
            if any(k in title for k in keywords):
                self.role_type = role
                break

        # Pre-fill responsibilities from job title if field is empty
        if not self.job_responsibilities and self.employee_id.job_title:
            self.job_responsibilities = self.employee_id.job_title

    job_responsibilities = fields.Text(
        string="Job Responsibilities",
        help="Describe this person's specific responsibilities. "
             "The AI uses this to tailor its daily and weekly advice.",
    )

    daily_coaching_enabled = fields.Boolean(
        string="Daily Coaching Message", default=True,
        help="Send OdooBot DM to this employee at 6PM with their day summary and role-specific advice.",
    )
    weekly_summary_enabled = fields.Boolean(
        string="Weekly Review Message", default=True,
        help="Send OdooBot DM to this employee every Monday at 6AM with a full week review.",
    )

    _sql_constraints = [
        ("unique_employee", "unique(employee_id)", "One coaching config per employee."),
    ]

    # ── Cron entry points ─────────────────────────────────────────────────────

    @api.model
    def _run_daily_coaching(self):
        """Called by attendance cron after generating summaries. Posts daily reports."""
        today = fields.Date.today()
        configs = self.search([("employee_id.active", "=", True)])

        # ── Manager channel: combined team post ──
        non_manager = configs.filtered(lambda c: c.role_type != "manager")
        if non_manager:
            manager_html = self._build_manager_daily_html(non_manager, today)
            self._post_to_manager_channel(manager_html)

        # ── Employee DMs ──
        for config in configs.filtered("daily_coaching_enabled"):
            try:
                summary = self.env["prema.attendance.summary"].search([
                    ("employee_id", "=", config.employee_id.id),
                    ("date", "=", today),
                ], limit=1)
                if not summary or summary.state == "no_data":
                    continue
                history = self._get_weekly_aggregates(config.employee_id, weeks=4)
                advice = self._generate_ai_advice(config, summary, history, period="daily")
                msg_html = self._build_employee_daily_html(config, summary, advice)
                self._post_bot_dm(config.employee_id, msg_html)
            except Exception as exc:
                _logger.error("Daily coaching failed for %s: %s", config.employee_id.name, exc)

    @api.model
    def _run_weekly_coaching(self):
        """Called every Monday 6AM EST. Posts weekly reviews."""
        today = date.today()
        # "last week" = Mon-Sun that just ended
        days_since_monday = today.weekday()  # Monday=0
        this_monday = today - timedelta(days=days_since_monday)
        last_monday = this_monday - timedelta(weeks=1)
        last_sunday = this_monday - timedelta(days=1)

        configs = self.search([("employee_id.active", "=", True)])

        # ── Manager channel: team week-in-review ──
        non_manager = configs.filtered(lambda c: c.role_type != "manager")
        if non_manager:
            manager_html = self._build_manager_weekly_html(
                non_manager, last_monday, last_sunday
            )
            self._post_to_manager_channel(manager_html)

        # ── Employee DMs ──
        for config in configs.filtered("weekly_summary_enabled"):
            try:
                this_week = self._get_weekly_aggregates(
                    config.employee_id, weeks=1,
                    ref_start=last_monday, ref_end=last_sunday
                )
                if not this_week:
                    continue
                history = self._get_weekly_aggregates(config.employee_id, weeks=4)
                advice = self._generate_ai_advice(config, None, history, period="weekly")
                msg_html = self._build_employee_weekly_html(
                    config, this_week[0], history, advice,
                    last_monday, last_sunday
                )
                self._post_bot_dm(config.employee_id, msg_html)
            except Exception as exc:
                _logger.error("Weekly coaching failed for %s: %s", config.employee_id.name, exc)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _get_weekly_aggregates(self, employee, weeks=4, ref_start=None, ref_end=None):
        """
        Return a list of weekly aggregate dicts (most recent first).
        If ref_start/ref_end given, uses that as week 1 then goes back.
        """
        if ref_end is None:
            ref_end = date.today() - timedelta(days=1)
        if ref_start is None:
            days_since_mon = ref_end.weekday()
            ref_start = ref_end - timedelta(days=days_since_mon)

        results = []
        for i in range(weeks):
            w_start = ref_start - timedelta(weeks=i)
            w_end = ref_end - timedelta(weeks=i)
            summaries = self.env["prema.attendance.summary"].search([
                ("employee_id", "=", employee.id),
                ("date", ">=", w_start),
                ("date", "<=", w_end),
                ("state", "=", "done"),
            ])
            if not summaries:
                results.append(None)
                continue
            days_with_data = len(summaries)
            results.append({
                "week_start": w_start,
                "week_end": w_end,
                "days": days_with_data,
                "total_logged_hours": sum(s.worked_hours for s in summaries),
                "total_active_hours": sum(s.active_hours for s in summaries),
                "total_idle_hours": sum(s.idle_hours for s in summaries),
                "avg_logged_per_day": round(sum(s.worked_hours for s in summaries) / max(days_with_data, 1), 2),
                "avg_active_per_day": round(sum(s.active_hours for s in summaries) / max(days_with_data, 1), 2),
                "total_calls_out": sum(s.calls_outgoing for s in summaries),
                "total_calls_in": sum(s.calls_incoming for s in summaries),
                "total_call_min": round(sum(s.call_duration_minutes for s in summaries), 1),
                "avg_calls_per_day": round(sum(s.calls_outgoing for s in summaries) / max(days_with_data, 1), 1),
                "total_messages": sum(s.messages_count for s in summaries),
                "total_emails": sum(s.emails_count for s in summaries),
                "total_files": sum(s.files_uploaded for s in summaries),
                "days_with_high_idle": sum(
                    1 for s in summaries
                    if s.worked_hours > 0 and s.idle_hours / s.worked_hours > IDLE_WARNING_PCT
                ),
            })
        return [r for r in results if r is not None]

    # ── AI generation ─────────────────────────────────────────────────────────

    def _generate_ai_advice(self, config, today_summary, history, period="daily"):
        """Call AI for role-specific advice. Returns HTML string."""
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import get_api_key as _get_deepseek_key
        api_key = _get_deepseek_key(self.env)
        if not api_key:
            return "<p><em>AI advice unavailable — API key not configured.</em></p>"

        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat

        system_prompt = ROLE_SYSTEM_PROMPTS.get(config.role_type, ROLE_SYSTEM_PROMPTS["manager"])
        if config.job_responsibilities:
            system_prompt += f"\n\nThis employee's specific responsibilities: {config.job_responsibilities}"

        # Build data block
        data_lines = []

        if period == "daily" and today_summary:
            s = today_summary
            data_lines.append(
                f"TODAY ({s.date}):\n"
                f"  Logged: {s.worked_hours:.1f}h | Active: {s.active_hours:.1f}h | Idle: {s.idle_hours:.1f}h\n"
                f"  Calls out: {s.calls_outgoing} ({s.call_duration_minutes:.0f} min) | "
                f"Calls in: {s.calls_incoming}\n"
                f"  Notes: {s.messages_count} | Emails: {s.emails_count} | Files: {s.files_uploaded}"
            )
            if s.idle_periods_text and "None" not in s.idle_periods_text:
                data_lines.append(f"  Idle gaps:\n    {s.idle_periods_text.replace(chr(10), chr(10)+'    ')}")

        if history:
            data_lines.append("\nPREVIOUS WEEKS (most recent first):")
            for i, w in enumerate(history[:4]):
                arrow_calls = ""
                if i > 0 and history[i - 1]:
                    prev_calls = history[i - 1]["total_calls_out"]
                    curr_calls = w["total_calls_out"]
                    arrow_calls = " ↑" if curr_calls > prev_calls else (" ↓" if curr_calls < prev_calls else " →")
                data_lines.append(
                    f"  Week of {w['week_start'].strftime('%b %d')}: "
                    f"{w['days']} days | "
                    f"avg {w['avg_active_per_day']:.1f}h active/day | "
                    f"{w['total_calls_out']} calls{arrow_calls} ({w['total_call_min']:.0f} min) | "
                    f"{w['total_messages']} notes | "
                    f"high-idle days: {w['days_with_high_idle']}"
                )

        user_prompt = (
            f"Employee: {config.employee_id.name}\n"
            f"Role: {ROLE_LABELS.get(config.role_type, config.role_type)}\n\n"
            + "\n".join(data_lines)
            + f"\n\nWrite a {'daily coaching message' if period == 'daily' else 'weekly review'} for {config.employee_id.name}.\n"
            "Include:\n"
            "1. What they did well today/this week (specific numbers)\n"
            "2. What needs improvement (specific and actionable, max 2 points)\n"
            "3. One concrete goal for tomorrow/next week\n"
            "4. If previous weeks show improvement, acknowledge it. If declining, address it.\n"
            "Keep it under 150 words. Professional, warm, direct. No fluff.\n"
            "Format as plain text — no headers, no bullet symbols, just flowing sentences."
        )

        try:
            return deepseek_chat(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=300,
                api_key=api_key,
            )
        except Exception as exc:
            _logger.error("AI coaching advice failed: %s", exc)
            return "AI coaching unavailable today."

    # ── Message builders — plain text, no tables, Discuss-safe ──────────────

    @staticmethod
    def _line(char="─", width=40):
        return char * width

    def _build_manager_daily_html(self, configs, report_date):
        now_toronto = datetime.now(TORONTO_TZ)
        parts = [
            f"<p><b>📊 Staff Daily Report — {report_date.strftime('%A %B %d, %Y')}</b></p>"
        ]
        for config in configs:
            summary = self.env["prema.attendance.summary"].search([
                ("employee_id", "=", config.employee_id.id),
                ("date", "=", report_date),
            ], limit=1)

            name = config.employee_id.name
            role = ROLE_LABELS.get(config.role_type, "")
            parts.append(f"<p><b>{'─'*36}</b><br/><b>{name}</b> · {role}</p>")

            if not summary or summary.state == "no_data":
                parts.append("<p>⚪ No attendance recorded today</p>")
                continue

            idle_pct = (summary.idle_hours / summary.worked_hours * 100) if summary.worked_hours else 0
            idle_flag = " ⚠️" if idle_pct > 40 else ""

            lines = [
                f"⏱ Logged: <b>{summary.worked_hours:.1f}h</b>"
                f"  ·  Active: <b>{summary.active_hours:.1f}h</b>"
                f"  ·  Idle: <b>{summary.idle_hours:.1f}h{idle_flag}</b>",
                f"📞 Calls: <b>{summary.calls_outgoing}</b> out"
                f"  ·  {summary.calls_incoming} in"
                f"  ·  {summary.call_duration_minutes:.0f} min",
                f"💬 Notes: {summary.messages_count}"
                f"  ·  📧 Emails: {summary.emails_count}",
            ]
            if summary.idle_periods_text and "None" not in summary.idle_periods_text:
                idle_lines = [l.strip() for l in summary.idle_periods_text.strip().split("\n") if l.strip()]
                lines.append("⏳ Idle gaps: " + "  |  ".join(idle_lines[:4]))

            parts.append("<p>" + "<br/>".join(lines) + "</p>")

        parts.append(
            f"<p><i>Generated {now_toronto.strftime('%I:%M %p')} EST"
            f" · <a href='/web#action=action_prema_attendance_summary'>Open Staff Summaries</a></i></p>"
        )
        return "".join(parts)

    def _build_manager_weekly_html(self, configs, week_start, week_end):
        now_toronto = datetime.now(TORONTO_TZ)
        parts = [
            f"<p><b>📈 Week in Review — {week_start.strftime('%b %d')}–{week_end.strftime('%b %d, %Y')}</b></p>"
        ]
        for config in configs:
            agg_list = self._get_weekly_aggregates(
                config.employee_id, weeks=1,
                ref_start=week_start, ref_end=week_end
            )
            name = config.employee_id.name
            role = ROLE_LABELS.get(config.role_type, "")
            parts.append(f"<p><b>{'─'*36}</b><br/><b>{name}</b> · {role}</p>")

            if not agg_list:
                parts.append("<p>⚪ No data this week</p>")
                continue

            w = agg_list[0]
            idle_flag = " ⚠️" if w["days_with_high_idle"] >= 2 else ""
            lines = [
                f"📅 Days active: <b>{w['days']}</b>",
                f"⏱ Avg active/day: <b>{w['avg_active_per_day']:.1f}h</b>",
                f"📞 Calls: <b>{w['total_calls_out']}</b> out"
                f"  ·  {w['total_calls_in']} in"
                f"  ·  {w['total_call_min']:.0f} min",
                f"💬 Notes: {w['total_messages']}"
                f"  ·  🔴 High-idle days: {w['days_with_high_idle']}{idle_flag}",
            ]
            parts.append("<p>" + "<br/>".join(lines) + "</p>")

        parts.append(
            f"<p><i>Generated {now_toronto.strftime('%I:%M %p')} EST Monday</i></p>"
        )
        return "".join(parts)

    def _build_employee_daily_html(self, config, summary, ai_advice):
        now_toronto = datetime.now(TORONTO_TZ)
        role_label = ROLE_LABELS.get(config.role_type, "")
        first_name = config.employee_id.name.split()[0]
        date_str = summary.date.strftime("%A, %B %d")

        idle_pct = (summary.idle_hours / summary.worked_hours * 100) if summary.worked_hours else 0
        idle_line = ""
        if summary.idle_hours > 0:
            idle_line = f"<br/>⏳ <b>{summary.idle_hours:.1f}h idle</b> — {idle_pct:.0f}% of your day had no recorded Odoo activity"

        lines = [
            f"<p><b>Hi {first_name} 👋</b> — Your PremaFirm Summary for <b>{date_str}</b></p>",
            "<p>",
            f"⏱ Logged: <b>{summary.worked_hours:.1f}h</b>"
            f"  ·  Active: <b>{summary.active_hours:.1f}h</b>{idle_line}<br/>",
            f"📞 Calls: <b>{summary.calls_outgoing}</b> out"
            f"  ·  {summary.calls_incoming} in"
            f"  ·  {summary.call_duration_minutes:.0f} min total<br/>",
            f"💬 Notes/Chatter: {summary.messages_count}"
            f"  ·  📧 Emails: {summary.emails_count}",
            "</p>",
            f"<p>{'─'*36}<br/><b>🎯 Coaching — {role_label}</b></p>",
            f"<p>{ai_advice}</p>",
            f"<p><i>PremaFirm · {now_toronto.strftime('%I:%M %p')} EST"
            f" · All Odoo activity is tracked.</i></p>",
        ]
        return "".join(lines)

    def _build_employee_weekly_html(self, config, this_week, history, ai_advice, week_start, week_end):
        now_toronto = datetime.now(TORONTO_TZ)
        role_label = ROLE_LABELS.get(config.role_type, "")
        first_name = config.employee_id.name.split()[0]
        w = this_week

        # Trend vs previous week
        prev = history[1] if len(history) > 1 else None
        trend_calls = ""
        trend_active = ""
        if prev:
            c_delta = w["total_calls_out"] - prev["total_calls_out"]
            a_delta = w["avg_active_per_day"] - prev["avg_active_per_day"]
            trend_calls = f"  ({'↑' if c_delta >= 0 else '↓'}{abs(c_delta)} vs last week)"
            trend_active = f"  ({'↑' if a_delta >= 0 else '↓'}{abs(a_delta):.1f}h vs last week)"

        # 4-week history block
        history_lines = []
        for i, hw in enumerate(history[:4]):
            label = ["This week ", "Last week ", "2 weeks ago", "3 weeks ago"][i]
            idle_flag = " ⚠️" if hw["days_with_high_idle"] >= 2 else ""
            history_lines.append(
                f"{label}: {hw['total_calls_out']} calls"
                f"  ·  {hw['avg_active_per_day']:.1f}h/day active"
                f"  ·  {hw['total_messages']} notes{idle_flag}"
            )

        parts = [
            f"<p><b>📈 {first_name}'s Week in Review</b><br/>"
            f"{week_start.strftime('%B %d')} – {week_end.strftime('%B %d, %Y')}</p>",
            "<p>",
            f"⏱ Avg active/day: <b>{w['avg_active_per_day']:.1f}h</b>{trend_active}<br/>",
            f"📞 Calls: <b>{w['total_calls_out']}</b> out"
            f"  ·  {w['total_calls_in']} in"
            f"  ·  {w['total_call_min']:.0f} min{trend_calls}<br/>",
            f"💬 Notes: {w['total_messages']}"
            f"  ·  🔴 High-idle days: {w['days_with_high_idle']}",
            "</p>",
            f"<p><b>📊 4-Week Trend</b><br/>" + "<br/>".join(history_lines) + "</p>",
            f"<p>{'─'*36}<br/><b>🎯 Weekly Coaching — {role_label}</b></p>",
            f"<p>{ai_advice}</p>",
            f"<p><i>PremaFirm weekly review · Monday {now_toronto.strftime('%I:%M %p')} EST"
            f" · Use this week to improve on the points above.</i></p>",
        ]
        return "".join(parts)

    # ── Channel helpers ────────────────────────────────────────────────────────

    def _get_odoobot_partner(self):
        """Get OdooBot's partner reliably."""
        for ref in ("mail.partner_root", "base.partner_root"):
            try:
                p = self.env.ref(ref)
                if p:
                    return p
            except Exception:
                pass
        return self.env["res.partner"].sudo().search(
            [("name", "=", "OdooBot")], limit=1
        )

    def _get_or_create_manager_channel(self):
        """Find or create the private admin-only performance reports channel."""
        ICP = self.env["ir.config_parameter"].sudo()
        channel_id_str = ICP.get_param("prema.staff_reports_channel_id", "")

        if channel_id_str:
            ch = self.env["discuss.channel"].browse(int(channel_id_str)).exists()
            if ch:
                return ch

        admin_user = self.env.ref("base.user_admin", raise_if_not_found=False)
        admin_partner = admin_user.partner_id if admin_user else False

        channel = self.env["discuss.channel"].sudo().create({
            "name": "Staff Performance Reports",
            "channel_type": "group",
            "description": "Private management channel — daily & weekly staff activity reports. Admin only.",
        })
        if admin_partner:
            self.env["discuss.channel.member"].sudo().create({
                "channel_id": channel.id,
                "partner_id": admin_partner.id,
            })
        ICP.set_param("prema.staff_reports_channel_id", str(channel.id))
        _logger.info("Created private management reports channel ID %s", channel.id)
        return channel

    def _get_or_create_bot_dm(self, employee):
        """Find or create the OdooBot DM channel for this employee."""
        user = employee.user_id
        if not user or not user.partner_id:
            return None

        bot_partner = self._get_odoobot_partner()
        if not bot_partner:
            return None

        employee_partner = user.partner_id

        # Search for existing chat channel between bot and employee
        self.env.cr.execute("""
            SELECT m1.channel_id
            FROM discuss_channel_member m1
            JOIN discuss_channel_member m2 ON m2.channel_id = m1.channel_id
            JOIN discuss_channel c ON c.id = m1.channel_id
            WHERE m1.partner_id = %s
              AND m2.partner_id = %s
              AND c.channel_type = 'chat'
            LIMIT 1
        """, (bot_partner.id, employee_partner.id))
        row = self.env.cr.fetchone()

        if row:
            return self.env["discuss.channel"].browse(row[0])

        # Create new DM channel — use partner_ids so Odoo creates members internally
        channel = self.env["discuss.channel"].sudo().with_context(
            mail_create_nosubscribe=True
        ).create({
            "channel_type": "chat",
            "name": f"OdooBot-{employee_partner.name}",
            "channel_member_ids": [
                (0, 0, {"partner_id": bot_partner.id}),
                (0, 0, {"partner_id": employee_partner.id}),
            ],
        })
        return channel

    def _post_to_channel(self, channel, html_body, author_partner=None):
        """Post a message to a discuss.channel using its own message_post."""
        if not channel:
            return
        bot_partner = author_partner or self._get_odoobot_partner()
        author_id = bot_partner.id if bot_partner else False
        try:
            channel.sudo().message_post(
                body=Markup(html_body),
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                author_id=author_id,
            )
        except Exception as exc:
            _logger.warning("_post_to_channel via message_post failed (%s), trying direct create", exc)
            # Fallback: direct SQL insert of mail.message
            self.env.cr.execute("""
                INSERT INTO mail_message
                    (model, res_id, message_type, subtype_id, author_id, body,
                     date, create_uid, write_uid, create_date, write_date)
                VALUES
                    ('discuss.channel', %s, 'comment',
                     (SELECT id FROM mail_message_subtype WHERE xml_id='mail.mt_comment' LIMIT 1),
                     %s, %s,
                     NOW(), 1, 1, NOW(), NOW())
            """, (channel.id, author_id, html_body))

    def _post_to_manager_channel(self, html_body):
        channel = self._get_or_create_manager_channel()
        self._post_to_channel(channel, html_body)

    def _post_bot_dm(self, employee, html_body):
        channel = self._get_or_create_bot_dm(employee)
        self._post_to_channel(channel, html_body)
