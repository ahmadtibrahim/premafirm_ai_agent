"""PHASE 28 — idempotent deployment repairs for noupdate=1 data.

All module data files ship noupdate="1", so on an ALREADY-INSTALLED
production DB an upgrade creates missing records but does NOT update
existing ones. This branch changed existing records in three places that
therefore need explicit repair at deploy time (never on fresh installs,
where the data files apply directly):

1. The six legacy CRM crons now ship ``active=False`` (PHASES 13-15 —
   superseded by the consolidated follow-up service / manual runs).
   Leaving them active on a deployed DB would silently re-enable
   production automation the program disabled (double sends, races).
2. The CDR sync cron code string (PHASE 25): 18.0.6.19.0 shipped
   ``model.fetch_from_voipms(days=2, download_recordings=True)``; the
   final signature is ``model.fetch_from_voipms(days=2)`` (only relevant
   to DBs that ever ran 18.0.6.19.0; fresh installs get the fixed XML).

Idempotent by construction: every step compares before writing, so a
second upgrade changes nothing. Nothing here deletes or archives
records, and no crm.lead data is touched — the pipeline restructure
remains the separately-approved PHASE 34 migration
(``crm.stage.action_premafirm_pipeline_restructure``, audit CSV
reviewed first).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# (xmlid, field, target) — applied only when the value differs.
_CRONS_TO_DISABLE = [
    'premafirm_ai_engine.ir_cron_contact_rotation',
    'premafirm_ai_engine.ir_cron_outreach_stale',
    'premafirm_ai_engine.ir_cron_replied_warning',
    'premafirm_ai_engine.ir_cron_replied_stale',
    'premafirm_ai_engine.ir_cron_crm_followup',
    'premafirm_ai_engine.ir_cron_cold_reactivation',
]

_CDR_CODE = 'model.fetch_from_voipms(days=2)'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in _CRONS_TO_DISABLE:
        cron = env.ref(xmlid, raise_if_not_found=False)
        if cron and cron.active:
            cron.active = False
            _logger.info('PHASE 28: %s deactivated (ships disabled)',
                         xmlid)
    cdr = env.ref('premafirm_ai_engine.ir_cron_voipms_cdr_sync',
                  raise_if_not_found=False)
    if cdr and cdr.code != _CDR_CODE:
        cdr.code = _CDR_CODE
        _logger.info('PHASE 28: CDR cron code set to %r', _CDR_CODE)
