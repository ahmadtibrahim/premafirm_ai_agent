"""PHASE 2 — injection hooks: every existing outbound path gets the
canonical threading headers with no core changes.

``mail.mail.create`` covers template sends (matrix path E), the bulk
wizard (path F), direct creates and notification-path mails — anything
that lands on mail.mail with a CRM-thread message already attached.
_notify comment-mode mails (paths A/B/C) already carry correct
References and are skipped (references non-empty).

``mail.template.send_mail`` is overridden as a belt-and-suspenders for
paths that create the mail outside the normal create flow.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class MailMail(models.Model):
    _inherit = 'mail.mail'

    @api.model_create_multi
    def create(self, vals_list):
        mails = super().create(vals_list)
        svc = self.env['premafirm.mail.threading']
        for mail in mails:
            if mail.mail_message_id and mail.mail_message_id.model == 'crm.lead':
                svc.normalize_mail(mail)
        return mails


class MailTemplate(models.Model):
    _inherit = 'mail.template'

    def send_mail(self, res_id, force_send=False, raise_exception=False,
                  email_values=None):
        mail_ids = super().send_mail(
            res_id,
            force_send=force_send,
            raise_exception=raise_exception,
            email_values=email_values,
        )
        svc = self.env['premafirm.mail.threading']
        for mail_id in (mail_ids if isinstance(mail_ids, (list, tuple)) else [mail_ids]):
            mail = self.env['mail.mail'].browse(mail_id)
            if mail and mail.mail_message_id and mail.mail_message_id.model == 'crm.lead':
                svc.normalize_mail(mail)
        return mail_ids
