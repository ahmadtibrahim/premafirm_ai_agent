"""PHASE 42 — remove Snov.io (and dead Apollo.io) from premafirm_ai_engine.

Snov.io subscription cancelled 2026-08-18. The models, fields, views, ACLs,
and taxonomy data are removed in this version's code; this migration cleans
up the runtime database where the ORM cannot:

1. Unlink the two contact-rotation crons (inactive on prod, but their
   records and ir.model.data xmlids persist in existing databases).
2. Delete the API credential parameters (Snov + Apollo) so the cancelled
   subscriptions' secrets no longer sit in ir.config_parameter.
3. Report the data rows that die with their dropped tables
   (premafirm.snov.contact, premafirm.crm.contact.rotation, taxonomy) —
   queried via raw SQL because the models are unregistered by this point.
4. Drop the orphaned tables. Odoo 18 leaves the tables behind when models
   are removed during an upgrade (the models leave the registry before
   `ir.model._drop_table` can run), so this migration drops them explicitly.
5. Delete the orphaned `ir.model.data` rows for the noupdate="1" snov
   taxonomy xmlids — noupdate records are never auto-cleaned.

Idempotent: safe on fresh installs (nothing to remove) and on databases
already upgraded. Credential VALUES are never logged — only key names.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_CRON_XMLIDS = (
    'premafirm_ai_engine.ir_cron_contact_rotation',
    'premafirm_ai_engine.cron_ml_crm_contact_rotation',
)
_CRON_NAME_FALLBACK = 'CRM: Contact Rotation — Suggest Next Contact / Snov Escalation'

_PARAM_KEYS = ('snov.client_id', 'snov.client_secret', 'apollo.api_key')

# Tables left behind by the ORM when the models were removed during upgrade
_ORPHAN_TABLES = (
    'premafirm_snov_industry',
    'premafirm_snov_company_size',
    'premafirm_snov_seniority',
    'premafirm_snov_job_title',
    'premafirm_snov_region',
    'premafirm_snov_contact',
    'premafirm_crm_contact_rotation',
    'premafirm_crm_outreach',
    'premafirm_apollo_contact',
    'apollo_search_wizard',
    'apollo_search_wizard_result',
    'snov_search_wizard',
    'snov_search_wizard_result',
)

_DROPPED_TABLES = (
    'premafirm_snov_contact',
    'premafirm_crm_contact_rotation',
    'premafirm_snov_industry',
    'premafirm_snov_company_size',
    'premafirm_snov_seniority',
    'premafirm_snov_job_title',
    'premafirm_snov_region',
    'premafirm_crm_outreach',
)


def _count_table_rows(cr, table):
    """Count rows in a table if it still exists (models are unregistered,
    so the ORM cannot see them; the table may already be dropped by the
    time this runs — that is expected and fine)."""
    cr.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = %s", (table,))
    if cr.fetchone()[0]:
        cr.execute('SELECT COUNT(*) FROM "%s"' % table)
        return cr.fetchone()[0]
    return None


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── 1. unlink the contact-rotation crons ───────────────────────────
    removed = []
    for xmlid in _CRON_XMLIDS:
        module, name = xmlid.split('.')
        imd = env['ir.model.data'].search([
            ('module', '=', module),
            ('name', '=', name),
            ('model', '=', 'ir.cron'),
        ], limit=1)
        if imd and imd.res_id:
            env['ir.cron'].browse(imd.res_id).unlink()
            removed.append(xmlid)
    # Fallback: the rotation cron predates clean xmlids in some DBs
    cron = env['ir.cron'].search(
        [('cron_name', '=', _CRON_NAME_FALLBACK)], limit=1)
    if cron:
        cron.unlink()
        removed.append('(by name) %s' % _CRON_NAME_FALLBACK)
    _logger.info('PHASE 42: contact-rotation cron(s) removed: %s',
                 ', '.join(removed) if removed else 'none found (already gone)')

    # ── 2. delete API credential parameters ────────────────────────────
    for key in _PARAM_KEYS:
        param = env['ir.config_parameter'].search([('key', '=', key)], limit=1)
        if param:
            _logger.info('PHASE 42: config parameter %s deleted', key)
            param.unlink()
        else:
            _logger.info('PHASE 42: config parameter %s not present', key)

    # ── 3. report rows that die with the dropped tables ────────────────
    for table in _DROPPED_TABLES:
        n = _count_table_rows(cr, table)
        if n is not None:
            _logger.info('PHASE 42: %s row(s) found in to-be-dropped table %s',
                         n, table)

    # ── 4. drop orphaned tables (see module docstring) ─────────────────
    for table in _ORPHAN_TABLES:
        cr.execute('DROP TABLE IF EXISTS "%s" CASCADE' % table)
        _logger.info('PHASE 42: orphan table %s dropped', table)

    # ── 5. delete orphaned snov taxonomy xmlids (noupdate=1) ───────────
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = 'premafirm_ai_engine' "
        "AND name LIKE 'snov\\_%'")
    _logger.info('PHASE 42: %s orphan snov xmlid(s) deleted',
                 cr.rowcount)

    _logger.info('PHASE 42 migration complete (Snov.io / Apollo removal)')
