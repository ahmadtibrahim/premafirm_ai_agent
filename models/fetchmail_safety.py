"""PHASES 6-7 — safe manual Fetch Now + duplicate inbound protection.

PHASE 6 (keep manual Fetch Now, but make it safe):
  * the Fetch Now button stays exactly where core puts it (form button
    ``fetch_mail`` — this module changes the BEHAVIOR, not the UI)
  * every fetch run (manual or cron) takes a PostgreSQL session-level
    advisory lock keyed on the server: concurrent runs (cron vs manual,
    two crons) never touch the same mailbox at the same time — a server
    whose lock is held is skipped and reported, never double-processed
  * every run is recorded in ``premafirm.fetchmail.run`` (source, times,
    per-server counts) — the audit log
  * per-server failure isolation: one mailbox blowing up never stops the
    others; the manual path shows the operator a result notification
  * ICP ``premafirm.crm_immediate_fetch_server_ids`` (comma-separated
    server ids, empty = all) restricts which servers a MANUAL fetch may
    touch — set it to [] to fully disable manual fetching

PHASE 7 (duplicate inbound protection):
  * ``premafirm.fetchmail.message`` records every IMAP message processed
    with its (uidvalidity, uid) — UNIQUE per server — the durable claim
    the spec requires; rows are written by a post-pass that re-connects
    once per run (core's loop is sequence-based and exposes no UIDs)
  * a content fingerprint (sha256 of Message-Id | From | Date | Subject
    | plaintext body) is claimed atomically in ``message_route`` BEFORE
    any routing: ``INSERT ... ON CONFLICT (fingerprint) DO NOTHING``
    blocks on an in-flight conflicting row, so even two mailboxes
    delivering the same mail at the same instant cannot both pass —
    the second copy is absorbed silently (no duplicate lead, no Replied,
    no needs_attention)
  * no core file changes (all hooks are overrides in this module)
"""
import hashlib
import logging
import re
import zlib

from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def _normalize(text):
    return re.sub(r'\s+', ' ', text or '').strip().lower()


def _raw_fingerprint(message):
    """sha256 over the RAW mail — identical across mailbox copies even
    when envelope headers (Received/Delivered-To) differ."""
    parts = []
    for part in message.walk():
        if part.get_content_type() == 'text/plain':
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode('utf-8', 'replace'))
    payload = '|'.join([
        (message.get('Message-Id') or '').strip(),
        (message.get('From') or '').strip().lower(),
        (message.get('Date') or '').strip(),
        (message.get('Subject') or '').strip().lower(),
        _normalize(' '.join(parts)),
    ])
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


class MailThreadDedupe(models.AbstractModel):
    """PHASE 7 — atomic fingerprint claim, before ANY routing.

    Imported AFTER ``inbound_routing`` so this override runs first in the
    MRO: a duplicate is absorbed here without ever reaching classification
    or route processing."""

    _inherit = 'mail.thread'

    @api.model
    def message_route(self, message, message_dict, model=None, thread_id=None,
                      custom_values=None):
        if not hasattr(message, 'get'):
            return super().message_route(message, message_dict, model,
                                         thread_id, custom_values)
        # Only the fetchmail path (cron AND manual Fetch Now — core sets
        # fetchmail_cron_running for both) participates in dedupe; direct
        # posts (mailgate, tests) are not deduped.
        if self.env.context.get('fetchmail_cron_running'):
            fingerprint = _raw_fingerprint(message)
            message_id = (message.get('Message-Id') or '').strip() or None
            server_id = self.env.context.get('default_fetchmail_server_id')
            # Atomic claim: blocks on an in-flight conflicting row; DO
            # NOTHING + no row returned => content already processed.
            self.env.cr.execute(
                """INSERT INTO premafirm_fetchmail_message
                       (server_id, uidvalidity, uid, message_id, fingerprint,
                        processed_at)
                   VALUES (%s, NULL, NULL, %s, %s, now())
                   ON CONFLICT (fingerprint) DO NOTHING
                   RETURNING id""",
                (server_id, message_id, fingerprint))
            if not self.env.cr.fetchone():
                _logger.info(
                    'premafirm.fetchmail: duplicate inbound dropped '
                    '(fingerprint %s, Message-Id %s) — already processed '
                    'elsewhere; no lead, no thread post',
                    fingerprint[:16], message_id or '?')
                return []
        return super().message_route(message, message_dict, model, thread_id,
                                     custom_values)


class FetchmailServer(models.Model):
    _inherit = 'fetchmail.server'

    def fetch_mail(self, raise_exception=True):
        """Manual Fetch Now (button) and cron path — made safe.

        One server at a time: advisory lock, manual-scope check, run audit,
        core processing, UID bookkeeping. The core loop is untouched —
        this wrapper changes only what surrounds it."""
        manual = raise_exception  # button default True, cron passes False
        results = {}
        for server in self:
            results[server.id] = self._safe_fetch_one(server, manual)
        if manual and results:
            ok = [k for k, v in results.items() if v == 'ok']
            not_ok = [k for k, v in results.items() if v != 'ok']
            summary = 'Fetched: %d server(s)' % len(ok)
            if not_ok:
                summary += ' · skipped/failed: %d' % len(not_ok)
            self.env.user._bus_send('notification', {
                'type': 'info',
                'title': 'Fetch Now',
                'message': summary,
                'sticky': False,
            })
        return True

    def _safe_fetch_one(self, server, manual):
        env = self.env
        lock_key = zlib.crc32(
            ('premafirm.fetchmail.%d' % server.id).encode()) & 0x7fffffff
        env.cr.execute('SELECT pg_try_advisory_lock(%s)', (lock_key,))
        if not env.cr.fetchone()[0]:
            # Another run (cron or manual) is already on this mailbox.
            _logger.info(
                'premafirm.fetchmail: %s server %s — a fetch is already '
                'running on it, skipping (no race)',
                server.server_type, server.name)
            return 'skipped (fetch already running)'
        try:
            if manual:
                allowed = (env['ir.config_parameter'].sudo().get_param(
                    'premafirm.crm_immediate_fetch_server_ids') or '').strip()
                if not allowed:
                    # PHASE 29 — the documented contract (module docstring,
                    # "set it to [] to fully disable manual fetching"): an
                    # EMPTY/absent config DISABLES manual Fetch Now on every
                    # server. Only the listed servers may be fetched by
                    # hand; the operator opts servers in explicitly.
                    _logger.info(
                        'premafirm.fetchmail: manual fetch skipped on '
                        '%s server %s (manual-fetch config is empty)',
                        server.server_type, server.name)
                    return 'skipped (not in manual-fetch config)'
                allowed_ids = {int(x) for x in allowed.split(',')
                               if x.strip()}
                if server.id not in allowed_ids:
                    _logger.info(
                        'premafirm.fetchmail: manual fetch skipped on '
                        '%s server %s (not in '
                        'premafirm.crm_immediate_fetch_server_ids)',
                        server.server_type, server.name)
                    return 'skipped (not in manual-fetch config)'
            run = env['premafirm.fetchmail.run'].sudo().create({
                'server_id': server.id,
                'source': 'manual' if manual else 'cron',
                'state': 'running',
            })

            # -- IMAP UID pre-pass (one extra connection; core opens its own)
            pre_uids, uidvalidity, conn = set(), 0, None
            pre_ok = None
            try:
                if server._get_connection_type() == 'imap':
                    conn = server.connect()
                    conn.select()
                    _typ, resp = conn.response('UIDVALIDITY')
                    if resp:
                        uidvalidity = int(resp[0].split()[-1])
                    _typ, data = conn.uid('search', None, '(UNSEEN)')
                    pre_uids = set((data[0] or b'').split())
                    pre_ok = True
            except Exception:
                _logger.warning(
                    'premafirm.fetchmail: UID pre-pass failed on server %s',
                    server.name, exc_info=True)
                pre_ok = False
            if pre_ok is False:
                # Connection is dead — core would only repeat the failure.
                # Record the failed run and move on to the next server.
                run.write({'state': 'failed'})
                env.cr.commit()
                env.cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
                return 'failed'

            # -- core processing, per server, failures isolated --
            failed = 0
            try:
                super(FetchmailServer, server).fetch_mail(
                    raise_exception=False)
            except Exception:
                _logger.error(
                    'premafirm.fetchmail: server %s run aborted',
                    server.name, exc_info=True)
                failed = -1

            # -- UID bookkeeping post-pass: claim every message the core
            #    just marked \\Seen that we saw unseen before --
            processed = set()
            if conn:
                try:
                    _typ, data = conn.uid('search', None, '(UNSEEN)')
                    post_uids = set((data[0] or b'').split())
                    processed = pre_uids - post_uids
                    for uid in processed:
                        try:
                            env.cr.execute(
                                """INSERT INTO premafirm_fetchmail_message
                                       (server_id, uidvalidity, uid,
                                        processed_at)
                                   VALUES (%s, %s, %s, now())
                                   ON CONFLICT (server_id, uidvalidity, uid)
                                   DO NOTHING""",
                                (server.id, uidvalidity, int(uid)))
                        except Exception:
                            env.cr.rollback()
                    conn.close()
                    conn.logout()
                except Exception:
                    _logger.warning(
                        'premafirm.fetchmail: UID post-pass failed on '
                        'server %s', server.name, exc_info=True)
            env.cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))

            if failed:
                run.write({'state': 'failed'})
            else:
                run.write({'state': 'done', 'new_count': len(processed)})
            env.cr.commit()
            result = 'failed' if failed else 'ok'
            _logger.info(
                'premafirm.fetchmail: %s run on %s server %s — %d processed, '
                '%d failed, run id %s',
                'manual' if manual else 'cron', server.server_type,
                server.name, len(processed), max(failed, 0), run.id)
            return result
        except Exception:
            _logger.error(
                'premafirm.fetchmail: server %s unexpected failure',
                server.name, exc_info=True)
            try:
                env.cr.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
            except Exception:
                pass
            return 'failed'


class PremafirmFetchmailRun(models.Model):
    _name = 'premafirm.fetchmail.run'
    _description = 'Fetchmail run audit (manual and cron)'
    _order = 'id desc'

    server_id = fields.Many2one('fetchmail.server', 'Server', required=True,
                                ondelete='cascade', index=True)
    source = fields.Selection([('manual', 'Manual (Fetch Now)'),
                               ('cron', 'Scheduled Cron')], required=True)
    state = fields.Selection([('running', 'Running'), ('done', 'Done'),
                              ('failed', 'Failed')], default='running',
                             required=True)
    new_count = fields.Integer('New Messages Processed')
    failed_count = fields.Integer('Failed Messages', default=0)
    create_date = fields.Datetime('Started At', readonly=True)
    write_date = fields.Datetime('Finished At', readonly=True)


class PremafirmFetchmailMessage(models.Model):
    _name = 'premafirm.fetchmail.message'
    _description = 'Processed inbound message claim (dedupe key)'
    _order = 'id desc'

    server_id = fields.Many2one('fetchmail.server', 'Server',
                                ondelete='cascade', index=True)
    uidvalidity = fields.Integer('UIDVALIDITY', index=True)
    uid = fields.Integer('UID')
    message_id = fields.Char('Message-Id', index=True)
    fingerprint = fields.Char('Content Fingerprint', index=True)
    processed_at = fields.Datetime('Processed At', index=True)

    _sql_constraints = [
        ('uid_unique_per_server',
         'UNIQUE (server_id, uidvalidity, uid)',
         'A message UID may only be claimed once per server.'),
        ('fingerprint_unique',
         'UNIQUE (fingerprint)',
         'The same inbound content may only be processed once.'),
    ]
