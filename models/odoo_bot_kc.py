"""
Phase 4 — OdooBot Knowledge Center override
Intercepts OdooBot chat messages and answers using the Business Profile + GPT
when the native OdooBot has nothing useful to say.
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)

# Minimum body length to consider it a real question
_MIN_BODY_LEN = 4


class MailBotKC(models.AbstractModel):
    _inherit = 'mail.bot'

    def _get_answer(self, record, body, values, command=False):
        """
        1. Let native OdooBot answer first.
        2. Only intercept if super() returned nothing.
        3. Build a GPT-powered response using the business profile.
        """
        # Step 1 — native OdooBot handler
        native_answer = super()._get_answer(record, body, values, command)
        if native_answer:
            return native_answer

        # Step 2 — gate checks
        try:
            if not body or len(body.strip()) < _MIN_BODY_LEN:
                return False

            # Only respond when the bot is in a private chat or pinged
            odoobot_id = self.env['ir.model.data']._xmlid_to_res_id('base.partner_root')
            if not odoobot_id:
                return False

            # Step 3 — load business profile
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            if not profile or not profile.company_overview:
                return False

            # Step 4 — OpenAI key and model
            ICP = self.env['ir.config_parameter'].sudo()
            api_key = ICP.get_param('openai.api_key')
            model = ICP.get_param('prema_ai.fast_model', 'gpt-4o-mini')
            if not api_key:
                return False

            # Step 5 — build system prompt and call cached GPT
            system_prompt = profile.get_system_prompt()
            system_prompt = (
                system_prompt
                + '\n\nYou are the internal AI assistant for PremaFirm Inc. '
                  'Answer questions about the company, its services, processes, '
                  'and logistics operations. Be concise and helpful.'
            )

            response = self.env['premafirm.ml.response.cache'].sudo().cached_ai_call(
                system_prompt=system_prompt,
                user_message=body.strip(),
                model=model,
                api_key=api_key,
                max_tokens=512,
            )
            return response or False

        except Exception as exc:
            _logger.debug('OdooBot KC override error (non-fatal): %s', exc)
            return False
