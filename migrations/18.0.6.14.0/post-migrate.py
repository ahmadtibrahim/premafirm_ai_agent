"""PHASE 13-14 — retarget DB-created stage references onto the new
pipeline; backfill the reply flags for rows the 18.0.6.13.0-era code
missed.

Runs after the module upgrade created the target stage records
(data/crm_stages.xml, noupdate=1 → created when missing). Idempotent:
re-running finds the automation already retargeted and skips.

What is fixed here (DB-created records have no xmlid — the XML files
cannot touch them):

* Automation 60 "CRM: Replied" (trigger on_message_received, write action
  ir_act_server 1390, fields_write → stage_id crm.stage,5 "Replied").
  After the PHASE 13 restructure "Replied" is archived with zero leads —
  new replies must land in the ENGAGED / REPLIED stage instead. The
  stage id is resolved BY NAME (stage records are configurable data).
* The legacy write action for New → Outreach (ir_act_server "CRM: Set
  Stage → Outreach") targets the archived "Outreach" stage by name in
  code — retargeted to OUTREACH SENT.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _retarget_write_action(env, action, old_name, new_name):
    """Swap the stage in an ir.actions.server object_write action
    (evaluation_type='value', value shaped 'crm.stage,<id>'). By name,
    never by id. Odoo 18 stores the write target in update_path /
    update_field_id and the value in the plain `value` field."""
    if not action or action.state != 'object_write':
        return False
    if action.update_path not in (False, 'stage_id'):
        return False
    if not action.update_field_id or action.update_field_id.name != 'stage_id':
        return False
    if action.evaluation_type != 'value':
        return False
    value = (action.value or '').strip()
    # many2one: the value column stores the plain id ('5'); the legacy
    # 'crm.stage,5' format is tolerated defensively.
    if ',' in value:
        value = value.rsplit(',', 1)[1].strip()
    if not value.isdigit():
        return False
    stage = env['crm.stage'].browse(int(value)).exists()
    if not stage or stage.name != old_name:
        return False
    target = env['crm.stage'].search([('name', '=', new_name)], limit=1)
    if not target:
        _logger.warning(
            'PHASE 13: target stage "%s" missing — %s not retargeted',
            new_name, action.name)
        return False
    action.write({'value': '%s' % target.id})
    _logger.info(
        'PHASE 13: write action "%s" retargeted %s → %s',
        action.name, old_name, new_name)
    return True


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. automation 60 "CRM: Replied" → ENGAGED / REPLIED ───────────
    automations = env['base.automation'].search([
        ('name', '=', 'CRM: Replied'),
        ('trigger', '=', 'on_message_received'),
        ('model_id.model', '=', 'crm.lead'),
    ])
    for automation in automations:
        for action in automation.action_server_ids:
            _retarget_write_action(env, action, 'Replied',
                                   'ENGAGED / REPLIED')

    # ── 2. New → Outreach write action → OUTREACH SENT ────────────────
    for action in env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'object_write'),
    ]):
        _retarget_write_action(env, action, 'Outreach', 'OUTREACH SENT')

    # ── 3. name-based server action code (Set Stage → Outreach) ───────
    for action in env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'code'),
    ]):
        code = action.code or ''
        if "'Outreach'" in code and "stage_id" in code:
            target = env['crm.stage'].search(
                [('name', '=', 'OUTREACH SENT')], limit=1)
            if target:
                action.write({
                    'code': code.replace("'Outreach'", "'OUTREACH SENT'"),
                })
                _logger.info(
                    'PHASE 13: code action "%s" retargeted to OUTREACH SENT',
                    action.name)

    # ── 4. backfill reply flags for rows written before the stored
    # computed flags existed (idempotent — only touches NULL/mismatched
    # rows that were not already corrected). ───────────────────────────
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
         WHERE reply_received != (last_meaningful_reply_at IS NOT NULL)
            OR waiting_on_customer != (
                last_outbound_at IS NOT NULL
                AND (last_inbound_at IS NULL
                     OR last_outbound_at >= last_inbound_at))
            OR needs_reply != (
                last_inbound_at IS NOT NULL
                AND (last_outbound_at IS NULL
                     OR last_inbound_at > last_outbound_at))
    """)
    _logger.info(
        'PHASE 13-14 migration complete (automations retargeted, '
        'reply flags backfilled)')
