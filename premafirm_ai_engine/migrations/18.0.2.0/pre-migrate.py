from odoo import SUPERUSER_ID, api

MODELS = [
    "crm.lead",
    "sale.order",
    "premafirm.ai.log",
    "premafirm.load",
]

FIELD_NAME = "billing_mode"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Remove field metadata
    fields = env["ir.model.fields"].search([
        ("name", "=", FIELD_NAME),
        ("model", "in", MODELS),
    ])

    if fields:
        fields.with_context(_force_unlink=True).unlink()

    # Drop physical columns safely
    for model in MODELS:
        table = model.replace(".", "_")

        cr.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='{table}'
                    AND column_name='{FIELD_NAME}'
                )
                THEN
                    ALTER TABLE {table} DROP COLUMN {FIELD_NAME};
                END IF;
            END$$;
        """)
