"""PHASE 17 — backfill the outbound analytics fields.

x_ana_outbound_count / first_outbound_at are maintained by the
``_message_post_after_hook`` on every outbound path from now on; this
migration backfills history from mail.message.

Outbound detection: ``email_outgoing`` is definitionally outbound
(mail.mail sends); ``email`` counts only when authored by an INTERNAL
user's partner (inbound customer mail is never counted).

Pure SQL (no ORM writes, no thread hooks) — idempotent (columns were just
created by the module load; the UPDATE only touches rows with NULL/0).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_SQL = """
UPDATE crm_lead l
SET x_ana_outbound_count = sub.c,
    first_outbound_at = sub.min_d
FROM (
    SELECT m.res_id,
           COUNT(*) AS c,
           MIN(m.date) AS min_d
    FROM mail_message m
    LEFT JOIN res_users u
           ON u.partner_id = m.author_id
          AND u.active IS TRUE
          AND u.share IS FALSE
    WHERE m.model = 'crm.lead'
      AND (m.message_type = 'email_outgoing'
           OR (m.message_type = 'email' AND u.id IS NOT NULL))
    GROUP BY m.res_id
) sub
WHERE l.id = sub.res_id
"""


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute(_SQL)
    affected = cr.rowcount
    _logger.info(
        'PHASE 17: backfilled outbound analytics for %s lead(s) '
        '(x_ana_outbound_count, first_outbound_at)', affected)
