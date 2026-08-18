"""PHASE 42 pre-migrate — scrub stale view archs before the module loads.

During a module upgrade Odoo re-writes and re-validates every module view;
validation runs against the FULL combined view tree built from the CURRENT
database state. Views whose arch still references elements removed in this
version (Snov button, rotation/snov fields, ICP taxonomy m2m fields) would
fail validation before their own XML files get a chance to reload.

This script strips those elements from the stored archs (raw SQL — the new
registry is not loaded yet, and the removed models/fields no longer exist).
Idempotent: patterns match nothing on fresh installs or already-clean DBs.

Removed elements scrubbed:
  <button name="action_snov_escalate_now" .../>          (crm.lead form header)
  <field name="x_rotation_count" .../>                   (AI assistant tab)
  <field name="x_snov_enrichment_requested" .../>        (AI assistant tab)
  <field name="icp_{industry,company_size,seniority,title,region}_ids" .../>
                                                        (business profile form)
"""
import json
import logging
import re

_logger = logging.getLogger(__name__)

_SCRUB_PATTERNS = (
    r'<button name="action_snov_escalate_now"[^>]*/>',
    r'<field name="x_rotation_count"[^>]*/>',
    r'<field name="x_snov_enrichment_requested"[^>]*/>',
    r'<field name="icp_(?:industry|company_size|seniority|title|region)_ids"[^>]*/>',
)

# SQL regex matching the union of all patterns (for the initial SELECT)
_SEARCH_RE = (
    r'action_snov_escalate_now|x_rotation_count|x_snov_enrichment_requested'
    r'|icp_(?:industry|company_size|seniority|title|region)_ids'
)


def migrate(cr, version):
    cr.execute(
        "SELECT id, arch_db FROM ir_ui_view WHERE arch_db::text ~ %s",
        (_SEARCH_RE,))
    rows = cr.fetchall()

    if not rows:
        _logger.info('PHASE 42: pre-migrate — no stale view archs found')
        return

    # Build the replacement arch as a Python dict and let the ORM-agnostic
    # json.dumps re-serialize; %s::jsonb keeps the column type intact.
    for view_id, arch_db in rows:
        if not isinstance(arch_db, dict):
            _logger.warning('PHASE 42: view %s arch_db not a jsonb map — skipping', view_id)
            continue
        new_arch = {}
        for lang, text in arch_db.items():
            if not isinstance(text, str):
                new_arch[lang] = text
                continue
            for pattern in _SCRUB_PATTERNS:
                text = re.sub(pattern, '', text)
            new_arch[lang] = text
        cr.execute(
            "UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s",
            (json.dumps(new_arch), view_id))
        _logger.info(
            'PHASE 42: pre-migrate scrubbed removed elements from view %s '
            '(arch languages: %s)', view_id, sorted(new_arch))

    _logger.info('PHASE 42: pre-migrate complete — %s view(s) scrubbed', len(rows))
