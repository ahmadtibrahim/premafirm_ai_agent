"""
Phase 3 — Response Cache
SHA-256 keyed cache for AI responses to avoid redundant GPT calls.
"""
import hashlib
import logging
import os
import sys

from odoo import api, fields, models
from datetime import timedelta

_logger = logging.getLogger(__name__)


class PremafirmMLResponseCache(models.Model):
    _name = 'premafirm.ml.response.cache'
    _description = 'ML Response Cache'

    prompt_hash = fields.Char(required=True, index=True, string='Prompt Hash')
    model_used = fields.Char(string='Model')
    response = fields.Text(string='Cached Response')
    hit_count = fields.Integer(default=0, string='Hit Count')
    expires_at = fields.Datetime(string='Expires At')

    _sql_constraints = [
        ('unique_hash', 'UNIQUE(prompt_hash)', 'Prompt hash must be unique.'),
    ]

    # ── Cache read ────────────────────────────────────────────────
    @api.model
    def get_cached(self, prompt_hash):
        """
        Return cached response string if it exists and has not expired.
        Increments hit_count on cache hit. Returns None on miss.
        """
        record = self.search([('prompt_hash', '=', prompt_hash)], limit=1)
        if not record:
            return None
        now = fields.Datetime.now()
        if record.expires_at and record.expires_at < now:
            # Expired — delete and treat as miss
            record.unlink()
            return None
        record.hit_count += 1
        return record.response

    # ── Cache write ───────────────────────────────────────────────
    @api.model
    def set_cache(self, prompt_hash, response, model_used='', ttl_days=30):
        """Upsert a cache entry by hash."""
        expires_at = fields.Datetime.now() + timedelta(days=ttl_days)
        existing = self.search([('prompt_hash', '=', prompt_hash)], limit=1)
        vals = {
            'prompt_hash': prompt_hash,
            'response': response,
            'model_used': model_used or '',
            'expires_at': expires_at,
        }
        if existing:
            existing.write(vals)
        else:
            self.create(vals)

    # ── Cached AI call (cache + GPT) ──────────────────────────────
    @api.model
    def cached_ai_call(self, system_prompt, user_message, model, api_key, max_tokens=1024):
        """
        Compute hash → check cache → call OpenAI if miss → store → return response.
        """
        hash_input = f'{model}|{system_prompt}|{user_message}'.encode('utf-8')
        prompt_hash = hashlib.sha256(hash_input).hexdigest()

        # Cache hit
        cached = self.get_cached(prompt_hash)
        if cached is not None:
            _logger.debug('ML cache hit for hash %s', prompt_hash[:16])
            return cached

        # Cache miss — call GPT
        services_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'services'
        )
        if services_path not in sys.path:
            sys.path.insert(0, services_path)
        from odoo.addons.premafirm_ai_engine.services.deepseek_utils import deepseek_chat

        response = deepseek_chat(
            messages=[{'role': 'user', 'content': user_message}],
            system=system_prompt,
            max_tokens=max_tokens,
            api_key=api_key,
            model=model,
        )

        # Store in cache
        try:
            self.set_cache(prompt_hash, response, model_used=model)
        except Exception as exc:
            _logger.debug('Cache write failed (non-fatal): %s', exc)

        return response

    # ── Cron: purge expired entries ───────────────────────────────
    @api.model
    def action_purge_expired(self):
        """Delete all cache records where expires_at is in the past."""
        now = fields.Datetime.now()
        expired = self.search([('expires_at', '<', now)])
        count = len(expired)
        expired.unlink()
        _logger.info('ML Response Cache: purged %d expired entries.', count)
