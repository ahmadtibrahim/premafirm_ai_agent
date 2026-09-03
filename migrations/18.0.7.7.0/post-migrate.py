"""18.0.7.7.0 — Issue 13: CRM automation fixes.

Deactivate the module's own on_write automation
``premafirm_ai_engine.automation_new_to_outreach`` (rule 4 in issue #13):
it moved any NEW / UNCONTACTED lead with complete contact details to
OUTREACH SENT on EVERY ordinary write (it has no watched field), so
contact edits, notes and chatter all advanced the stage.  From this
version the stage advances only from a genuine outbound customer email
posted on the lead (models/crm_reply_status.py hook) — a code path, not
a rule record.  The data file carries ``active="False"`` for fresh
installs; this migration flips existing databases.

No Studio rules are touched here (they are deactivated by ops only after
the replacement is deployed — see the mapping on issue #13).
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE base_automation ba
           SET active = FALSE
          FROM ir_model_data imd
         WHERE imd.model = 'base.automation'
           AND imd.module = 'premafirm_ai_engine'
           AND imd.name = 'automation_new_to_outreach'
           AND imd.res_id = ba.id
        """
    )
