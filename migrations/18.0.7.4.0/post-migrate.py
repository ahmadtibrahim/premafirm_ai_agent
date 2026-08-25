"""WEBSITE CRM LEAD CREATION — stage + company/contact hierarchy + dedupe.

Automation 63 ("Callback Request - tag, notify, note", crm.lead on_create)
was landing website /request-a-callback submissions in the OUTREACH SENT
stage (retargeted there by 18.0.6.15.0 when the old hardcoded stage 18 was
archived). Customer-initiated quote requests belong in QUOTE REQUESTED.
The same server action also created the company partner from the raw
form payload — whose free-text company field browser autofill fills with
a full "Company, Street, City, PROV, Canada" address — and linked the
lead to the CONTACT instead of the COMPANY.

This migration:
  1. rewrites the callback server action (ir.actions.server 1515) onto
     the new Python pipeline crm.lead.prema_process_website_callback()
     (normalization, address/name split, company→contact hierarchy with
     duplicate prevention, QUOTE REQUESTED stage by name, informative
     title — see models/crm_lead_extension.py);
  2. widens automation 63's filter so it keeps matching the legacy
     "Callback Request" title AND the new "{Company} — Quote Request"
     titles the pipeline now writes.
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import safe_eval

_logger = logging.getLogger(__name__)

_NEW_CALLBACK_CODE = (
    "for lead in records:\n"
    "    lead.prema_process_website_callback()\n"
)

_OLD_FILTER = [('team_id', '=', 1), ('name', '=', 'Callback Request')]
_NEW_FILTER = [
    ('team_id', '=', 1),
    '|',
    ('name', '=', 'Callback Request'),
    ('name', 'ilike', 'Quote Request'),
]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. rewrite the callback server action onto the pipeline ─────────
    actions = env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'code'),
    ])
    for action in actions:
        code = action.code or ''
        if 'Callback request is missing a required field' in code:
            action.write({'code': _NEW_CALLBACK_CODE})
            _logger.info(
                '18.0.7.4.0: callback server action "%s" (%s) rewritten '
                'onto crm.lead.prema_process_website_callback()',
                action.name, action.id)

    # ── 2. widen automation 63's filter for the informative titles ──────
    automations = env['base.automation'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('trigger', '=', 'on_create'),
        ('active', '=', True),
    ])
    for auto in automations:
        # filter_domain is a stored Char holding the repr() of the domain
        # (compute='_compute_filter_domain', store=True) — parse it before
        # comparing, never iterate the raw string.
        if safe_eval(auto.filter_domain or '[]') == _OLD_FILTER:
            auto.write({'filter_domain': repr(_NEW_FILTER)})
            _logger.info(
                '18.0.7.4.0: callback automation "%s" (%s) filter widened '
                'to match legacy + "Quote Request" titles',
                auto.name, auto.id)

    # ── 3. sanity ───────────────────────────────────────────────────────
    still_old = env['ir.actions.server'].search([
        ('model_id.model', '=', 'crm.lead'),
        ('state', '=', 'code'),
    ]).filtered(
        lambda a: 'Callback request is missing a required field'
        in (a.code or ''))
    _logger.info(
        '18.0.7.4.0 migration complete (%d callback action(s) still on '
        'legacy code)', len(still_old))
