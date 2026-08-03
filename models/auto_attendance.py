"""
Auto Attendance — forced check-in on Odoo login, auto check-out on disconnect.

Check-in  : triggered when bus.presence status → 'online' (first client ping after login)
Check-out : cron every 15 min — closes open attendance records for users offline > 30 min
"""
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

CHECKOUT_GRACE_MINUTES = 30  # close attendance this many minutes after last bus poll


class BusPresenceAutoAttendance(models.Model):
    _inherit = "bus.presence"

    def write(self, vals):
        # Capture old statuses before the write so we can detect online transitions
        old_statuses = {rec.id: rec.status for rec in self} if "status" in vals else {}
        result = super().write(vals)
        if "status" in vals and vals["status"] == "online":
            for rec in self:
                if old_statuses.get(rec.id) != "online" and rec.user_id:
                    self._auto_checkin_user(rec.user_id)
        return result

    def _auto_checkin_user(self, user):
        """Create an hr.attendance check-in for the employee linked to this user, if not already open."""
        try:
            employee = self.env["hr.employee"].search(
                [("user_id", "=", user.id), ("active", "=", True)], limit=1
            )
            if not employee:
                return

            # Skip if already checked in (open record with no check_out)
            open_att = self.env["hr.attendance"].sudo().search(
                [("employee_id", "=", employee.id), ("check_out", "=", False)], limit=1
            )
            if open_att:
                return

            now = fields.Datetime.now()
            self.env["hr.attendance"].sudo().create({
                "employee_id": employee.id,
                "check_in": now,
            })
            _logger.info("Auto check-in: %s at %s", employee.name, now)
        except Exception as exc:
            _logger.warning("Auto check-in failed for user %s: %s", user.login, exc)

    @api.model
    def _auto_checkout_offline_employees(self):
        """
        Cron: runs every 15 minutes.
        Closes any open hr.attendance record where the employee has been
        offline (bus.presence.last_poll older than CHECKOUT_GRACE_MINUTES) for too long.
        Also repairs phantom open records left from crashes or direct DB changes.
        """
        now = datetime.utcnow()
        grace_cutoff = now - timedelta(minutes=CHECKOUT_GRACE_MINUTES)
        yesterday_cutoff = now - timedelta(hours=20)  # safety net: anything older than 20h is stale

        open_atts = self.env["hr.attendance"].sudo().search([("check_out", "=", False)])
        closed = 0

        for att in open_atts:
            user = att.employee_id.user_id
            if not user:
                # No linked user — close with last known check-in + 8h as fallback
                att.sudo().write({"check_out": att.check_in + timedelta(hours=8)})
                closed += 1
                continue

            presence = self.env["bus.presence"].search(
                [("user_id", "=", user.id)], limit=1
            )

            # Case 1: No presence record → user has never used the browser, close it
            if not presence:
                att.sudo().write({"check_out": att.check_in + timedelta(hours=8)})
                closed += 1
                continue

            # Case 2: User is online right now → leave it open
            if presence.status == "online":
                continue

            # Case 3: last_poll is older than grace period → they've disconnected
            last_poll = presence.last_poll
            if last_poll and last_poll < grace_cutoff:
                checkout_time = last_poll
                att.sudo().write({"check_out": checkout_time})
                _logger.info("Auto check-out: %s at %s (last poll: %s)",
                             att.employee_id.name, checkout_time, last_poll)
                closed += 1
                continue

            # Case 4: Check-in is more than 20 hours ago (phantom/stale) → close it
            if att.check_in < yesterday_cutoff:
                # Use last_presence as the checkout time
                checkout_time = presence.last_presence or (att.check_in + timedelta(hours=9))
                att.sudo().write({"check_out": checkout_time})
                _logger.info("Auto check-out (stale): %s — closing %s-old session",
                             att.employee_id.name, now - att.check_in)
                closed += 1

        if closed:
            _logger.info("Auto check-out cron: closed %d attendance record(s)", closed)
        return closed
