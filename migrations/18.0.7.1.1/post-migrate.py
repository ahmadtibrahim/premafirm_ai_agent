"""PHASE 43 follow-up — retarget automation filter_domain (string-aware).

18.0.7.1.0's post-migrate iterated base_automation.filter_domain as if it
were a Python list, but the field is a Char storing repr() of the domain —
so the 'New' clause was never detected and automation 64 kept its legacy
filter. This migration is string-aware and idempotent; re-running is safe.

No lead is ever moved, no stage is ever deleted — only the automation's
stored filter string is rewritten to the canonical stage name.
"""
import ast
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    automations = env['base.automation'].search([
        ('name', 'ilike', 'CRM: New'),
        ('trigger', '=', 'on_write'),
        ('model_id.model', '=', 'crm.lead'),
    ])
    for automation in automations:
        raw = automation.filter_domain or ''
        domain = []
        if isinstance(raw, str) and raw.strip().startswith('['):
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
                'PHASE 43.1: automation %s filter_domain retargeted to '
                'NEW / UNCONTACTED', automation.id)
        else:
            _logger.info('PHASE 43.1: automation %s already canonical '
                         '(no change)', automation.id)
