"""PHASE 2 — Canonical outbound mail service.

Every CRM outbound email path flows through this service so that:

* every delivered mail carries RFC 5322 ``References`` built from STORED
  openerp message-ids — the only identifier that survives the Resend/SES
  Message-ID rewrite and can be matched by the inbound router
  (PHASE 1 matrix report, findings 1-4),
* ``Reply-To`` returns to the thread's inbound mailbox — a mail.alias the
  fetchmail pulls — so replies that lose their threading headers still
  reach the routing boundary instead of a dead address,
* a signed per-thread Reply-To suffix (``local+pf<id>-<checksum>@domain``)
  can be enabled once the mail server supports plus addressing (Postfix
  ``recipient_delimiter``); OFF by default — cannot be verified from
  outside the mail server and would require a production server change
  (deferred to PHASE 33 real UAT),
* nothing is ever matched by email address alone: a reply that cannot be
  threaded is queued for human review (premafirm.inbound.queue), never
  auto-linked and never a new lead.

Existing paths reach the service through ``mail.mail.create`` /
``mail.template.send_mail`` hooks (models/mail_send_hooks.py); new paths
(PHASES 9/10/15) call ``build_mail_values`` / ``normalize_mail`` directly.
No core file changes.
"""
import hashlib
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Same public-discussion types core _notify prefers when building threads.
_OUTGOING_TYPES = ('comment', 'auto_comment', 'email', 'email_outgoing')

# Reply-To values that point nowhere useful and must be redirected.
_BAD_REPLY_TO_MARKERS = ('odoobot@example.com', 'notifications@', 'postmaster@')


class PremafirmMailThreading(models.AbstractModel):
    _name = 'premafirm.mail.threading'
    _description = 'Canonical CRM mail threading service'

    @api.model
    def _icp(self, key, default=None):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    # ── Thread identity ────────────────────────────────────────────

    @api.model
    def _thread_inbox_address(self, thread):
        """Address replies to this thread should return to.

        Must be a mail.alias Odoo's fetchmail pulls back — never a bare
        send identity. Replies go to the team alias (accounts@premafirm.com),
        matching the Reply-To Odoo's own comment-mode composer already emits.
        (Tag-based segment routing removed 2026-08-20 — inbound routing is
        unchanged; fetchmail still pulls every mailbox listed above.)
        """
        if not thread or thread._name != 'crm.lead':
            return False
        alias = getattr(thread, 'team_id', False) and thread.team_id.alias_id
        if alias and alias.alias_domain_id and alias.alias_name:
            return '%s@%s' % (alias.alias_name, alias.alias_domain_id.name)
        return self._icp('premafirm.mail.reply_to_inc', 'accounts@premafirm.com')

    @api.model
    def _signed_reply_suffix(self, thread):
        """Signed per-thread suffix for the Reply-To local part, e.g.
        ``pf123-ab12cd``. Returns False while plus addressing is not
        enabled (ICP premafirm.mail.plus_suffix_enabled)."""
        if self._icp('premafirm.mail.plus_suffix_enabled', 'False') != 'True':
            return False
        secret = self._icp('premafirm.mail.plus_suffix_secret', '') or 'premafirm-thread-suffix'
        digest = hashlib.sha256(
            ('%s:%s:%s' % (thread._name, thread.id, secret)).encode()
        ).hexdigest()[:6]
        return 'pf%d-%s' % (thread.id, digest)

    @api.model
    def _reply_to_address(self, thread, base_address=None):
        base = base_address or self._thread_inbox_address(thread)
        if not base or '@' not in base:
            return base
        suffix = self._signed_reply_suffix(thread)
        if not suffix:
            return base
        local, domain = base.rsplit('@', 1)
        return '%s+%s@%s' % (local, suffix, domain)

    # ── RFC 5322 threading ─────────────────────────────────────────

    @api.model
    def _build_references(self, message):
        """Stored-id References for an outgoing message: the last public
        history messages of its thread plus the message itself — the same
        selection core _notify uses, capped at 32 for RFC 5322."""
        if not message:
            return False
        msg = message.sudo()
        ancestors = self.env['mail.message'].sudo().search([
            ('model', '=', msg.model),
            ('res_id', '=', msg.res_id),
            ('id', '!=', msg.id),
            ('subtype_id', '!=', False),  # filters out logs
            ('message_id', '!=', False),
        ], limit=32, order='id DESC')
        history = ancestors.sorted(lambda m: (
            not m.is_internal and not m.subtype_id.internal,
            m.message_type in _OUTGOING_TYPES,
            m.message_type != 'user_notification',
        ), reverse=True)[:3].sorted('id')  # oldest → newest
        return ' '.join(m.message_id for m in (history + msg))

    @api.model
    def _last_inbound_message(self, thread):
        """The most recent customer inbound on the thread — the parent a
        reply should point at."""
        return thread.message_ids.filtered(
            lambda m: m.message_type in ('email', 'email_incoming')
            and m.author_id and not m.author_id.user_ids
        ).sorted('id', reverse=True)[:1]

    # ── PHASE 22 — reply recipient discipline ──────────────────────

    @api.model
    def _reply_recipients(self, thread, mode='reply'):
        """Recipient set for replying on a CRM thread, computed from the
        LAST INBOUND message — never guessed from the lead record.

        'reply'     → the original sender only (Reply to Sender)
        'reply_all' → the sender + every other partner on the last
                      inbound (Odoo 18 keeps recipients as partner_ids —
                      no to/cc headers on mail.message anymore). When the
                      inbound carries no resolvable partners, reply_all
                      DEGRADES to reply — never blind-copies strangers.

        Returns (email_to, email_cc) — (False, False) when the thread
        has no inbound to reply to."""
        last = self._last_inbound_message(thread)
        if not last:
            return False, False
        author = last.author_id
        sender = (author.email_formatted if author and author.email
                  else False) or (last.email_from or False)
        if not sender:
            return False, False
        if mode != 'reply_all':
            return sender, False
        others = (last.partner_ids - author) if author else last.partner_ids
        cc = ', '.join(p.email_formatted for p in others if p.email)
        return sender, cc or False

    # ── PHASE 23 — sender identity ─────────────────────────────────

    @api.model
    def _sender_identity(self, thread=None, user=None):
        """From address for CRM outbound sends.

        Business decision 2026-08-20: always Odoo's own default sender —
        no tag/segment/salesperson override. Returns False so the caller
        leaves ``email_from`` unset and mail.mail resolves it the normal
        way (author's mailbox, else the company email). Reply-To is
        unaffected: replies return to the thread mailbox (PHASE 2-3)."""
        return False

    # ── Canonical mail values ──────────────────────────────────────

    @api.model
    def build_mail_values(self, thread, subject, body, email_to=None,
                          email_from=None, parent_id=None, reply_to=None,
                          references=None, reply_mode=None, **extra):
        """mail.mail values for a CRM-thread outbound email, with the
        canonical Reply-To and (optional) References. The single place
        header policy lives for new code paths.

        PHASE 22: ``reply_mode`` ('reply'/'reply_all') computes email_to
        (+email_cc) from the LAST INBOUND message and sets parent_id to
        it, when email_to is not given explicitly.
        PHASE 23: ``email_from=None`` resolves to Odoo's default sender
        (override removed 2026-08-20); False keeps Odoo's default."""
        if reply_mode and not email_to:
            email_to, cc = self._reply_recipients(thread, reply_mode)
            if cc:
                extra['email_cc'] = cc
            parent = self._last_inbound_message(thread)
            if parent:
                parent_id = parent.id
        if not email_to:
            raise ValueError(
                'build_mail_values: no recipient (thread %s has nothing '
                'to reply to?)' % (thread and thread.id or '-'))
        values = {
            'subject': subject,
            'body_html': body,
            'email_to': email_to,
        }
        if email_from is None:
            email_from = self._sender_identity(thread)
        if email_from:
            values['email_from'] = email_from
        if parent_id:
            values['parent_id'] = parent_id
        good_reply_to = reply_to or self._reply_to_address(thread)
        if good_reply_to:
            values['reply_to'] = good_reply_to
        if references:
            values['references'] = references
        values.update(extra)
        return values

    @api.model
    def normalize_mail(self, mail):
        """Idempotent post-create header fix for CRM-thread mails:
        fill missing References with stored ids, redirect an empty or
        dead Reply-To to the thread mailbox. Safe to run more than once;
        never raises."""
        if not mail or not mail.mail_message_id:
            return mail
        try:
            msg = mail.mail_message_id.sudo()
            if msg.model != 'crm.lead':
                return mail
            vals = {}
            if not (mail.references or '').strip():
                refs = self._build_references(msg)
                if refs:
                    vals['references'] = refs
            thread = self.env['crm.lead'].browse(msg.res_id).exists()
            reply_to = (mail.reply_to or '').strip()
            if thread and (not reply_to or any(
                m in reply_to.lower() for m in _BAD_REPLY_TO_MARKERS
            )):
                good = self._reply_to_address(thread)
                if good:
                    vals['reply_to'] = good
            if vals:
                mail.sudo().write(vals)
        except Exception as exc:
            # Header normalization must never block a send.
            _logger.warning('normalize_mail skipped for mail %s: %s', mail.id, exc)
        return mail
