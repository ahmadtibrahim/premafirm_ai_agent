"""PHASE 8 — gate "CRM: Replied" automation to normal_reply; backfill flags.

Runs after the module fields exist. Idempotent: re-running on an upgraded
database finds the automation already gated and backfills nothing.

Automation 60 ("CRM: Replied", model crm.lead, trigger on_message_received,
action: stage_id → Replied) is DB-created (no xmlid), so it is fixed here.
The gate MUST be `filter_pre_domain`: the message-trigger path in
base_automation monkey-patches message_post and evaluates only _filter_pre
(filter_domain/"Apply on" is never consulted for on_message_received).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_GATE = "[('last_inbound_classification', '=', 'normal_reply')]"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. gate the automation ────────────────────────────────────────
    automations = env['base.automation'].search([
        ('name', '=', 'CRM: Replied'),
        ('trigger', '=', 'on_message_received'),
        ('model_id.model', '=', 'crm.lead'),
    ])
    for automation in automations:
        if automation.filter_pre_domain != _GATE:
            automation.write({
                # the ONLY domain the message-trigger path evaluates
                'filter_pre_domain': _GATE,
                # same condition, visible in the automation UI for humans
                'filter_domain': _GATE,
            })
            _logger.info(
                'PHASE 8: gated automation "%s" (#%s) to normal_reply only',
                automation.name, automation.id)
        else:
            _logger.info(
                'PHASE 8: automation "%s" (#%s) already gated — no-op',
                automation.name, automation.id)

    # ── 2. backfill reply status from the legacy studio field ─────────
    # Idempotent: only fills rows that have no canonical value yet.
    cr.execute("""
        UPDATE crm_lead
           SET last_meaningful_reply_at = x_reply_received_at,
               last_inbound_classification = 'normal_reply'
         WHERE x_reply_received_at IS NOT NULL
           AND last_meaningful_reply_at IS NULL
    """)
    cr.execute("""
        UPDATE crm_lead
           SET last_inbound_at = COALESCE(last_inbound_at,
                                          last_meaningful_reply_at)
         WHERE last_meaningful_reply_at IS NOT NULL
           AND last_inbound_at IS NULL
    """)
    # Recompute the stored derived flags for existing rows (the ORM will
    # keep them correct for all future writes).
    cr.execute("""
        UPDATE crm_lead SET
            reply_received = (last_meaningful_reply_at IS NOT NULL),
            waiting_on_customer = (
                last_outbound_at IS NOT NULL
                AND (last_inbound_at IS NULL
                     OR last_outbound_at >= last_inbound_at)),
            needs_reply = (
                last_inbound_at IS NOT NULL
                AND (last_outbound_at IS NULL
                     OR last_inbound_at > last_outbound_at))
    """)
    _logger.info('PHASE 8: reply-status backfill complete '
                 '(%d automation(s) gated)', len(automations))
