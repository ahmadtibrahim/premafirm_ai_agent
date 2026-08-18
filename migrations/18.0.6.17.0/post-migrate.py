"""PHASE 18 — apply the CRM tag merge map (runs after module load so the
crm.tag.run_tag_cleanup code is available).

Rules honored: merge map is name-resolved (never ids); every retag is
audited in premafirm.crm.tag.audit (lead / old / new / source); obsolete
tags are ARCHIVED (active=False), never deleted. Idempotent — re-running
the same version is a no-op (obsolete tags are already archived and
leads already carry the canonical tag).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    summary = env['crm.tag'].run_tag_cleanup(
        dry_run=False, source='migration')
    lines = ['%s: create=%s leads=%s archive=%s' % (
        k, v['created'], v['leads'], ','.join(v['archived']))
        for k, v in summary.items()]
    _logger.info('PHASE 18: tag cleanup applied: %s', ' | '.join(lines))
