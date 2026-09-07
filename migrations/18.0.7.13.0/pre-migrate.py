"""18.0.7.13.0 pre-migrate — drop the legacy 'AI Rate Quote' CRM header view.

The legacy generative-AI pricing flow was removed in this version (the
action_ml_rate_quote handler on crm.lead, generate_rate_quote, the wa_reply
path).  Its carrier — the engine-owned inherited ir.ui.view
view_crm_lead_ml_buttons, which injects the header button(s) into the CRM
form — was dropped from the module XML at the same time, but the DB row
survives every upgrade because Odoo does not garbage-collect view records
that disappear from module data.

While the row survives, ANY engine upgrade that rewrites a crm.lead form
view re-validates the combined tree and fails on the stale arch — the
button references a method that no longer exists:

    <button name="action_ml_rate_quote" ...>  → "not a valid action on crm.lead"

(older snapshots carry a sibling action_ml_draft_reply button in the same
arch — both are removed with the record.)  This must run as a PRE-migration:
form re-validation happens while this module's data files load, which is
after 'pre' but before 'post'.

Raw SQL, matching the PHASE-42 precedent: the new registry is not loaded
yet.  Idempotent: the xmlid lookup is a no-op on fresh installs and on DBs
where the view was already dropped.
"""

import logging

_logger = logging.getLogger(__name__)

# Engine-owned ir.ui.view records whose ONLY content was the removed legacy
# generative-AI header buttons.  Handled by xmlid so DBs whose historical
# version carried slightly different archs (e.g. an extra action_ml_draft_reply
# button on older snapshots) are cleaned identically.
_LEGACY_BUTTON_VIEWS = (
    ("premafirm_ai_engine", "view_crm_lead_ml_buttons"),
)


def migrate(cr, version):
    if not version:
        # Fresh install — the record was never created (no XML anymore).
        return
    for module, xmlid in _LEGACY_BUTTON_VIEWS:
        cr.execute(
            "SELECT id FROM ir_model_data "
            "WHERE module=%s AND name=%s AND model='ir.ui.view'",
            (module, xmlid))
        row = cr.fetchone()
        if not row:
            _logger.info("18.0.7.13.0: legacy button view %s.%s already gone",
                         module, xmlid)
            continue
        cr.execute("SELECT id FROM ir_ui_view WHERE id=%s", (row[0],))
        if not cr.fetchone():
            cr.execute(
                "DELETE FROM ir_model_data "
                "WHERE module=%s AND name=%s AND model='ir.ui.view'",
                (module, xmlid))
            _logger.info("18.0.7.13.0: orphan ir_model_data for %s.%s cleaned",
                         module, xmlid)
            continue
        cr.execute("DELETE FROM ir_model_data "
                   "WHERE module=%s AND name=%s AND model='ir.ui.view'",
                   (module, xmlid))
        cr.execute("DELETE FROM ir_ui_view WHERE id=%s", (row[0],))
        _logger.info("18.0.7.13.0: dropped legacy CRM button view %s.%s (id %s)",
                     module, xmlid, row[0])
