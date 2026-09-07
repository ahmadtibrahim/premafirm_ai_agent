"""18.0.7.12.0 — Remove the obsolete 'AI Reply' wizard leftovers.

This version drops the AI Reply / AI Draft Reply estimate-drafting
wizard (``premafirm.crm.ai.reply.wizard``) from the module code (work
order TODO 6).  Databases upgraded from earlier versions still hold the
wizard's records unless they are cleaned up here, after the registry has
been reloaded without the wizard's model:

* the wizard's ``ir.ui.view`` record(s), found through their xmlids,
* the leftover ``ir.model.data`` rows (form-view xmlid, access-rule
  xmlid and the auto-created ``ir.model`` xmlid), so the records are not
  re-created by a later ``-i`` run,
* the ``ir.model.access`` rule whose CSV row was removed,
* the ``ir.model`` row — the attached ``ir.model.fields`` /
  ``ir.model.access`` / ``ir.model.constraint`` / ``ir.model.relation``
  rows all cascade through their ``model_id`` foreign keys,
* the wizard's SQL tables (transient model + attachment M2M).

Every statement is guarded: fresh installs (``version`` is None) and
databases where the rows are already gone must never fail the upgrade.
"""

import logging

_logger = logging.getLogger(__name__)

_WIZARD_MODEL = 'premafirm.crm.ai.reply.wizard'
_WIZARD_TABLE = 'premafirm_crm_ai_reply_wizard'
_REL_TABLE = 'premafirm_ai_reply_wizard_attachment_rel'


def migrate(cr, version):
    """Drop the leftover records of the removed AI Reply wizard."""
    if not version:
        return

    def _safe(sql, args=()):
        try:
            cr.execute(sql, args)
        except Exception as exc:
            # Leftover cleanup must never hard-fail an upgrade.
            _logger.warning(
                'premafirm_ai_engine: AI-reply-wizard cleanup skipped %s: %s',
                sql.split(None, 1)[0], exc)

    # 1. The wizard's ir.ui.view record(s), found through their xmlids
    #    (name contains 'ai_reply', e.g. view_crm_ai_reply_wizard_form).
    _safe(
        """
        DELETE FROM ir_ui_view
         WHERE id IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 'premafirm_ai_engine'
                AND name LIKE '%ai_reply%')
        """
    )

    # 2. The ir.model.access rule removed from ir.model.access.csv.
    _safe(
        """
        DELETE FROM ir_model_access
         WHERE model_id IN (SELECT id FROM ir_model WHERE model = %s)
        """,
        (_WIZARD_MODEL,),
    )

    # 3. Leftover ir.model.data rows for the wizard (form-view xmlid,
    #    access-rule xmlid and auto-created ir.model xmlid).  Done after
    #    steps 1-2 so the res_id lookups still resolve.
    _safe(
        """
        DELETE FROM ir_model_data
         WHERE module = 'premafirm_ai_engine'
           AND name LIKE '%ai_reply%'
        """
    )

    # 4. The ir.model row — the model class is gone from the code, so
    #    this is the only way the base rows (fields, access, constraints,
    #    relations — all ondelete cascade via model_id) get removed.
    _safe("DELETE FROM ir_model WHERE model = %s", (_WIZARD_MODEL,))

    # 5. The wizard's SQL tables: the transient model itself and the
    #    attachment M2M relation table.
    _safe('DROP TABLE IF EXISTS %s' % _REL_TABLE)
    _safe('DROP TABLE IF EXISTS %s' % _WIZARD_TABLE)
