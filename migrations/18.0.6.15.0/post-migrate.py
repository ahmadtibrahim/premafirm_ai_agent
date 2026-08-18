"""PHASE 15 — consolidate the six legacy follow-up crons into the one
Follow-Up Service; retarget the website-callback stage reference.

The legacy crons are MODULE records (noupdate=1 XML) — the XML flips in
this version ship them inactive for fresh installs, and this migration
deactivates them on existing databases. They are deactivated, never
deleted (history preserved).

Crons deactivated (by exact name, idempotent):
  CRM: AI Follow-up Draft Generator         (113)
  CRM: Cold Lead Reactivation               (114)
  CRM: Replied Stage — 3-Day Follow-up Warning (115)
  CRM: Replied Stage — 6-Day Stale → Data Collection (116)
  CRM: Contact Rotation — Suggest Next Contact / Snov Escalation (117)
  CRM: Outreach/Contacted — No Reply → Data Collection (118)

Retarget: the website-callback server action (automation 63's "Execute
Code", ir.actions.server 1515) hardcodes `env['crm.stage'].browse(18)`
— stage "Call Back", ARCHIVED by the PHASE 13 restructure. Callback
requests are customer-initiated contact without an email reply, so they
move to OUTREACH SENT (name-resolved, never by id) — matching the
PHASE 13 mapping for the Call Back segment (intelligent split with no
reply history → OUTREACH SENT + segment tag).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_LEGACY_CRON_NAMES = (
    'CRM: AI Follow-up Draft Generator',
    'CRM: Cold Lead Reactivation',
    'CRM: Replied Stage — 3-Day Follow-up Warning',
    'CRM: Replied Stage — 6-Day Stale → Data Collection',
    'CRM: Contact Rotation — Suggest Next Contact / Snov Escalation',
    'CRM: Outreach/Contacted — No Reply → Data Collection',
)

_STALE_BROWSE = "env['crm.stage'].browse(18)"
_RETARGETED = (
    "call_back_stage = env['crm.stage'].search("
    "[('name', '=', 'OUTREACH SENT')], limit=1)"
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. deactivate the six legacy follow-up crons ───────────────────
    crons = env['ir.cron'].search([('cron_name', 'in', list(_LEGACY_CRON_NAMES)),
                                   ('active', '=', True)])
    for cron in crons:
        cron.write({'active': False})
        _logger.info('PHASE 15: legacy cron deactivated: %s', cron.cron_name)

    # ── 2. retarget the website-callback stage (hardcoded id → name) ───
    actions = env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'code'),
    ])
    for action in actions:
        code = action.code or ''
        if _STALE_BROWSE in code:
            action.write({'code': code.replace(_STALE_BROWSE, _RETARGETED)})
            _logger.info(
                'PHASE 15: code action "%s" retargeted off archived '
                'stage id 18 (Call Back) → OUTREACH SENT by name',
                action.name)

    # ── 3. sanity log ──────────────────────────────────────────────────
    still_active = env['ir.cron'].search_count([
        ('cron_name', 'in', list(_LEGACY_CRON_NAMES)),
        ('active', '=', True)])
    _logger.info(
        'PHASE 15 migration complete (legacy crons deactivated, callback '
        'stage retargeted; %s legacy cron(s) still active)',
        still_active)
