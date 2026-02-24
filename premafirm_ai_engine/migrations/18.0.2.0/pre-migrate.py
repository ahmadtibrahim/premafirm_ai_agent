from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    fields = env["ir.model.fields"].search([
        ("name", "=", "billing_" + "mode"),
        ("model", "=", "crm.lead"),
    ])

    if fields:
        fields.with_context(_force_unlink=True).unlink()

    column_name = "billing_" + "mode"
    cr.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='crm_lead'
                AND column_name='{column_name}'
            ) THEN
                ALTER TABLE crm_lead DROP COLUMN {column_name};
            END IF;
        END$$;
    """
    )
