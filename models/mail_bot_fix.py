"""
OdooBot auto-reply suppression.

Problem: When Ahmad sends Grace a direct message in Discuss, OdooBot
intercepts the conversation and auto-responds with greetings/commands.

Fix: Suppress OdooBot responses in DM channels that contain 2+ real
internal users. OdooBot still works normally when a user messages it
directly in the OdooBot-dedicated DM channel.

Preserved behaviors:
- OdooBot responds normally in its own dedicated DM with any user
- OdooBot system notifications (assignments, mentions) still work
- OdooBot still responds to commands in its own thread
- All group channels are unaffected
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class MailBotAutoReplyFix(models.AbstractModel):
    _inherit = "mail.bot"

    def _post_answer_if_needed(self, messages):
        """Skip OdooBot auto-reply when 2+ real users are talking in a DM."""
        bot_partner = self._get_bot_partner_safe()

        # If we can't identify OdooBot, fall back to default behavior
        if not bot_partner:
            return super()._post_answer_if_needed(messages)

        # Filter messages: keep only those OdooBot should respond to
        filtered = messages.filtered(
            lambda msg: not self._should_suppress(msg, bot_partner)
        )

        if not filtered:
            return
        return super()._post_answer_if_needed(filtered)

    def _should_suppress(self, message, bot_partner):
        """
        Return True if OdooBot should stay silent for this message.
        Suppress when: DM channel ('chat') between 2+ real internal users.
        """
        if message.model not in ("mail.channel", "discuss.channel"):
            return False

        channel = self.env["discuss.channel"].browse(message.res_id).exists()
        if not channel or channel.channel_type != "chat":
            return False

        # Don't suppress OdooBot's own messages
        if message.author_id.id == bot_partner.id:
            return False

        # Count real internal users (not OdooBot, not portal/public)
        members = channel.channel_member_ids.mapped("partner_id")
        real_internal = members.filtered(
            lambda p: p.id != bot_partner.id
            and p.user_ids
            and not p.user_ids.filtered(lambda u: u.share or not u.active)
        )

        # If 2+ real internal users → this is a staff DM, suppress bot
        return len(real_internal) >= 2

    def _get_bot_partner_safe(self):
        for ref in ("mail.partner_root", "base.partner_root"):
            try:
                p = self.env.ref(ref, raise_if_not_found=False)
                if p:
                    return p
            except Exception:
                pass
        return self.env["res.partner"].sudo().search(
            [("name", "=", "OdooBot")], limit=1
        )
