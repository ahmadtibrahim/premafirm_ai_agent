"""PHASE 43 — Pipeline cleanup completion (UAT-first; runs on upgrade).

Two leftovers from the PHASE 13 restructure, both on UAT and production:

1. The live base.automation "CRM: New → Outreach (contact details
   complete)" (xmlid premafirm_ai_engine.automation_new_to_outreach)
   still filters on the ARCHIVED stage name 'New'. The record is
   noupdate=1, so the corrected XML in data/crm_workflow_automations.xml
   does not touch the live row — this migration retargets it. The
   linked server action (action_server_new_to_outreach) was already
   retargeted by 18.0.6.14.0; we re-verify it here idempotently.

2. Stage "Approved" (a DB-created legacy stage, no xmlid) is not
   folded, so it still renders as a pipeline column even though every
   lead was remapped (Approved → WON / ACTIVE CUSTOMER by
   crm_pipeline._OLD_TO_NEW).

Safety: stages are ARCHIVED (fold=True, moved to the end of the
sequence) — never deleted. A legacy stage is only folded when it holds
ZERO active leads; if any active lead is still on it, the stage is left
untouched and reported (human decision required). No lead is ever moved
by this migration.
"""
import ast
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Exact legacy stage names (case-sensitive — 'Onboarding' is the legacy
# stage; 'ONBOARDING' is canonical and must never be touched here).
_LEGACY_STAGES = {
    'New', 'Outreach', 'Contacted', 'Replied', 'Data Collection',
    'Onboarding', 'Approved', 'Lost / Cancelled', 'Paused', 'Call Back',
    'Call Approach', 'Dedicated Corridors Campaign', 'Retail',
    'Suggestion',
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. Retarget automation filter_domain to the canonical stage ──
    automations = env['base.automation'].search([
        ('name', 'ilike', 'CRM: New'),
        ('trigger', '=', 'on_write'),
        ('model_id.model', '=', 'crm.lead'),
    ])
    for automation in automations:
        raw = automation.filter_domain or ''
        domain = []
        if isinstance(raw, str) and raw.strip().startswith('['):
            # filter_domain is a Char field storing repr() of the domain
            try:
                domain = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                domain = []
        elif isinstance(raw, list):
            domain = raw
        fixed = []
        changed = False
        for clause in domain:
            if (isinstance(clause, (list, tuple)) and len(clause) == 3
                    and clause[0] == 'stage_id.name'
                    and clause[1] == '=' and clause[2] == 'New'):
                clause = ['stage_id.name', '=', 'NEW / UNCONTACTED']
                changed = True
            fixed.append(clause)
        if changed:
            automation.write({'filter_domain': repr(fixed),
                              'name': 'CRM: NEW / UNCONTACTED → OUTREACH SENT (contact details complete)'})
            _logger.info(
                'PHASE 43: automation %s filter_domain retargeted to '
                'NEW / UNCONTACTED', automation.id)
        else:
            _logger.info('PHASE 43: automation %s already canonical '
                         '(no change)', automation.id)

    # ── 2. Re-verify the linked server action resolves OUTREACH SENT ──
    for action in env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'code'),
    ]):
        code = action.code or ''
        if "'Outreach'" in code and "stage_id" in code:
            action.write({'code': code.replace("'Outreach'", "'OUTREACH SENT'")})
            _logger.info('PHASE 43: code action %s retargeted to OUTREACH SENT',
                         action.id)

    # ── 3. Fold legacy stages that still render as pipeline columns ──
    folded = []
    skipped = []
    stages = env['crm.stage'].search([])
    for stage in stages:
        if stage.name not in _LEGACY_STAGES:
            continue
        if stage.fold:
            continue
        active_leads = env['crm.lead'].search_count([
            ('stage_id', '=', stage.id),
            ('active', '=', True),
        ])
        if active_leads:
            skipped.append((stage.name, active_leads))
            _logger.warning(
                'PHASE 43: legacy stage "%s" still holds %d active '
                'leads — NOT folded (human decision required)',
                stage.name, active_leads)
            continue
        max_seq = max((s.sequence or 0) for s in stages) + 1
        stage.write({'fold': True, 'sequence': max_seq + len(folded)})
        folded.append(stage.name)
        _logger.info('PHASE 43: legacy stage "%s" folded (archived)',
                     stage.name)

    _logger.info('PHASE 43: folded stages=%s skipped_with_leads=%s',
                 folded or 'none', skipped or 'none')
