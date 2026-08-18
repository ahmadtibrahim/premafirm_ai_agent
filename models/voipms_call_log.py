"""PHASE 25 — VoIP.ms CDR pull on the CRM.

The voipms_sms module records calls via the Asterisk CDR webhook
(``log_call``) but has no API path: call history that happened before
the webhook existed — or while it was down — was missing. This adds the
read-only VoIP.ms ``getCDR`` pull (verified live against the account on
2026-08-18):

* upserts ``voipms.call.log`` rows by ``asterisk_uniqueid`` (webhook and
  API records share the same table, no duplicates),
* resolves the contact by phone AND the CRM lead via the partner, so the
  call history surfaces on the lead,
* skips the provider's ``Call Recording`` marker rows (they are their
  own CDR entries, not calls).

RECORDINGS — REPORTED AS BLOCKED: the VoIP.ms v1 REST API does not
expose recording downloads (``recording=1``/``recording=yes`` return no
URL field; the only trace is the .mp3 FILENAME inside the ``useragent``
of ``Call Recording`` rows). The ``recording`` binary on
``voipms.call.log`` therefore keeps flowing from the existing Asterisk
webhook path (``log_call(recording_b64=...)``); an API backfill is
blocked by the provider and is NOT implemented.

The pull is strictly READ-ONLY against the VoIP.ms account (a plain
``getCDR`` GET — the same credentials the module already uses to send
SMS). The sync cron ships disabled; the operator enables it on
deployment. Missing credentials or provider errors return
``{'status': 'blocked', ...}`` instead of raising — the "report if
blocked" behavior of this phase.
"""
import logging
import re
from datetime import timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

VOIPMS_API = 'https://voip.ms/api/v1/rest.php'

# VoIP.ms getCDR disposition → voipms.call.log.call_status
_CDR_STATUS = {
    'ANSWERED': 'answered',
    'NO ANSWER': 'missed',
    'BUSY': 'busy',
    'FAILED': 'failed',
    'CANCEL': 'missed',
    'CONGESTION': 'failed',
}

_PHONE_IN_BRACKETS = re.compile(r'<(\d+)>')


class VoipmsCallLog(models.Model):
    _inherit = 'voipms.call.log'

    lead_id = fields.Many2one('crm.lead', 'Lead', ondelete='set null',
                              index=True,
                              help='CRM lead linked via the caller\'s '
                                   'contact (partner of the call).')

    # ── config ──────────────────────────────────────────────────────

    @api.model
    def _voipms_params(self):
        """(username, password, did) from the module's own config — the
        same params voipms_sms uses for SMS. Never hardcoded."""
        p = self.env['ir.config_parameter'].sudo()
        return (p.get_param('voipms_sms.api_username'),
                p.get_param('voipms_sms.api_password'),
                p.get_param('voipms_sms.did'))

    # ── CDR pull ────────────────────────────────────────────────────

    @api.model
    def fetch_from_voipms(self, days=2):
        """Pull recent call records from VoIP.ms (read-only getCDR).

        Idempotent: rows are upserted on ``asterisk_uniqueid``; re-runs
        update instead of duplicating. The provider caps responses at 90
        records — the default 2-day window stays under it; widen by
        passing a smaller ``days``. Returns a result dict — never raises
        on provider/network errors (reported as ``status`` so a failing
        cron does not cascade)."""
        username, password, did = self._voipms_params()
        if not (username and password and did):
            _logger.warning('PHASE 25: VoIP.ms CDR sync blocked — '
                            'credentials not configured')
            return {'status': 'blocked',
                    'reason': 'voipms_sms API credentials not configured'}
        now = fields.Datetime.now()
        params = {
            'api_username': username,
            'api_password': password,
            'method': 'getCDR',
            'date_from': (now - timedelta(days=days)).strftime('%Y-%m-%d'),
            'date_to': now.strftime('%Y-%m-%d'),
            'did': did,
            'timezone': -4,  # Eastern — the account's own zone
            # the status filters are required params (one or more):
            'answered': 1, 'noanswer': 1, 'busy': 1, 'failed': 1,
        }
        try:
            data = requests.get(VOIPMS_API, params=params,
                                timeout=30).json()
        except Exception as exc:
            _logger.error('PHASE 25: VoIP.ms getCDR request failed: %s',
                          exc)
            return {'status': 'blocked',
                    'reason': 'request failed: %s' % exc}
        status = data.get('status')
        if status not in ('success', 'no_cdr'):
            # no_cdr is a legitimate EMPTY result, not an error
            _logger.warning('PHASE 25: VoIP.ms getCDR status %s', status)
            return {'status': 'blocked',
                    'reason': 'voip.ms status: %s' % status}
        created = updated = skipped = 0
        for raw in data.get('cdr') or []:
            # provider-internal "Call Recording" rows are not calls
            if str(raw.get('description') or '').lower() \
                    == 'call recording':
                skipped += 1
                continue
            uid = str(raw.get('uniqueid') or '').strip()
            if not uid:
                skipped += 1
                continue
            vals = self._cdr_vals(raw)
            existing = self.sudo().search(
                [('asterisk_uniqueid', '=', uid)], limit=1)
            if existing:
                existing.sudo().write(vals)
                updated += 1
            else:
                self.sudo().create(vals)
                created += 1
        _logger.info('PHASE 25: VoIP.ms CDR sync: %d created, %d updated, '
                     '%d skipped (no recordings via API — provider '
                     'blocked, webhook only)',
                     created, updated, skipped)
        return {'status': 'ok', 'created': created, 'updated': updated,
                'skipped': skipped,
                'recordings': 'blocked_by_provider'}

    @api.model
    def _cdr_vals(self, raw):
        """Map one live VoIP.ms getCDR row (verified shape: date,
        callerid, destination, description, disposition, seconds,
        uniqueid, destination_type) → voipms.call.log values."""
        raw_date = str(raw.get('date') or '').replace('T', ' ')
        try:
            date = fields.Datetime.from_string(raw_date)
        except Exception:
            date = fields.Datetime.now()
        disposition = str(raw.get('disposition') or '').upper()
        status = _CDR_STATUS.get(disposition, 'missed')
        try:
            seconds = int(raw.get('seconds') or 0)
        except (TypeError, ValueError):
            seconds = 0
        description = str(raw.get('description') or '')
        dest_type = str(raw.get('destination_type') or '')
        # inbound when the provider says so (description "Inbound DID",
        # destination_type "IN:..."), else outbound
        inbound = dest_type.startswith('IN') \
            or description.lower().startswith('inbound')
        # the customer number: caller-id for inbound, destination for
        # outbound (destination is our own DID on inbound)
        callerid = str(raw.get('callerid') or '')
        m = _PHONE_IN_BRACKETS.search(callerid)
        caller_num = m.group(1) if m else \
            ''.join(c for c in callerid if c.isdigit())
        customer = caller_num if inbound else \
            str(raw.get('destination') or '')
        partner = False
        if customer:
            pid = self._partner_by_digits(customer)
            if pid:
                partner = self.env['res.partner'].browse(pid)
        vals = {
            'date': date,
            'caller_number': customer or '',
            'direction': 'inbound' if inbound else 'outbound',
            'extension': '',
            'call_status': status,
            'duration': seconds,
            'asterisk_uniqueid': str(raw.get('uniqueid') or '').strip(),
        }
        if partner:
            vals['partner_id'] = partner.id
            vals['lead_id'] = self._lead_of(partner)
        # answered_by is NOT in the API response (extension is only known
        # to the Asterisk webhook) — the webhook path keeps setting it.
        return vals

    @api.model
    def _partner_by_digits(self, digits_10):
        digits = ''.join(c for c in (digits_10 or '') if c.isdigit())
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        if len(digits) != 10:
            return False
        self.env.cr.execute("""
            SELECT id FROM res_partner
            WHERE active = true
              AND (
                regexp_replace(phone,   '[^0-9]', '', 'g') LIKE %s
                OR regexp_replace(mobile, '[^0-9]', '', 'g') LIKE %s
              )
            ORDER BY id DESC LIMIT 1
        """, ['%' + digits, '%' + digits])
        row = self.env.cr.fetchone()
        return row and row[0] or False

    @api.model
    def _lead_of(self, partner):
        lead = self.env['crm.lead'].sudo().search([
            ('partner_id', '=', partner.id),
            ('type', '=', 'lead'),
            ('active', '=', True),
        ], order='date_open desc', limit=1)
        return lead.id or False

    # ── CRM actions ─────────────────────────────────────────────────

    def action_sync_cdr(self):
        """Manual 'Sync CDR' button — runs the pull and reports the
        outcome. Users have no write access on voipms.call.log, so the
        fetch runs sudo (read-only against VoIP.ms)."""
        result = self.env['voipms.call.log'].sudo().fetch_from_voipms()
        if result.get('status') == 'ok':
            note = ('VoIP.ms CDR sync: %(created)d new, %(updated)d '
                    'updated. Recordings are not downloadable via the '
                    'VoIP.ms API — they arrive from the Asterisk webhook.'
                    % result)
            level = 'success'
        elif result.get('status') == 'blocked':
            note = 'VoIP.ms CDR sync blocked: %s' % result.get('reason')
            level = 'warning'
        else:
            note = 'VoIP.ms CDR sync failed: %s' % result.get('reason')
            level = 'danger'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'Call Sync', 'message': note,
                       'type': level, 'sticky': False},
        }
