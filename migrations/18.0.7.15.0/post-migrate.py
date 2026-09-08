"""18.0.7.15.0 post-migrate — sync the stored AI role prompt to the code default.

The singleton profile's ai_role_prompt is auto-populated only when EMPTY
(get_profile), so DBs created before a default-text edit keep running the
legacy prompt forever.  The 2026-09-08 ASK-AI wave ships a stricter EMAIL
DRAFT FORMAT (email-only output, no invented history, internal notes never
disclosed) inside DEFAULT_ROLE_PROMPT; without this migration the stored row
still carries the 2026-05 text that told the model to print 'Objective /
Account insight / Recommended next action' sections around drafted emails.

Rows that were deliberately customized (stored text that does not start with
the default's own '=== YOUR ROLE ===' marker) are left untouched.  Idempotent:
once synced, the stored value starts with the same marker and is rewritten to
the same default on any later run of this migration.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import SUPERUSER_ID, api
    from odoo.addons.premafirm_ai_engine.models.business_profile import (
        DEFAULT_ROLE_PROMPT,
    )
    env = api.Environment(cr, SUPERUSER_ID, {})
    profiles = env['premafirm.business.profile'].sudo().search([])
    updated = 0
    for profile in profiles:
        stored = (profile.ai_role_prompt or '').strip()
        if not stored or stored.startswith('=== YOUR ROLE ==='):
            profile.ai_role_prompt = DEFAULT_ROLE_PROMPT
            updated += 1
    _logger.info(
        '18.0.7.15.0: synced ai_role_prompt on %d profile row(s)', updated)
