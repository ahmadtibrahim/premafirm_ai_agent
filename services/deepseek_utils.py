"""
DeepSeek API helper — OpenAI-compatible chat completions.
Drop-in replacement for openai_utils.openai_chat() and claude_utils.claude_chat().

Endpoint: https://api.deepseek.com/v1/chat/completions
Auth:     Bearer token from system parameter 'deepseek.api_key'
Format:   OpenAI-compatible (same as openai_utils)

Retry behaviour (same as openai_utils):
  429 / 500 / 502 / 503 / 504 — back-off and retry
  Up to MAX_RETRIES attempts, doubling delay each time.
"""

import logging
import time
from datetime import datetime

import pytz
import requests

_logger = logging.getLogger(__name__)


def today_context_line(tz_name="America/Toronto"):
    """A one-line 'today's actual date' anchor to prepend to any prompt that
    asks the model to resolve a relative date ('tomorrow', 'next Monday') or
    invent a missing one. Without an explicit anchor the model has no way to
    know the real current date and silently falls back to its own guess.
    """
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("America/Toronto")
    now_local = datetime.now(pytz.utc).astimezone(tz)
    return f"Today's actual date is {now_local.strftime('%A, %B %d, %Y')} ({tz_name}). Use this as \"today\" for any relative date — never assume a different year."

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 60.0


def get_api_key(env):
    """Read DeepSeek API key from system parameters."""
    ICP = env["ir.config_parameter"].sudo()
    # Try 'deepseek.api_key' first, fall back to 'deepseek'
    return (
        ICP.get_param("deepseek.api_key")
        or ICP.get_param("deepseek")
        or ""
    ).strip()


def get_model(env):
    """Read preferred DeepSeek model from system parameters.
    Checks: deepseek.model → prema_ai.fast_model → prema_ai.primary_model → DEFAULT_MODEL
    """
    ICP = env["ir.config_parameter"].sudo()
    return (
        ICP.get_param("deepseek.model")
        or ICP.get_param("prema_ai.fast_model")
        or ICP.get_param("prema_ai.primary_model")
        or DEFAULT_MODEL
    )


def deepseek_chat(
    messages,
    system=None,
    max_tokens=1024,
    api_key=None,
    model=None,
    timeout=60,
    env=None,
):
    """
    Call DeepSeek chat completions and return response text.

    ``messages`` follows OpenAI format:
      [{"role": "user", "content": "..."}, ...]

    If ``api_key`` is not provided, reads from system param 'deepseek.api_key'.
    If ``model`` is not provided, reads from system param 'deepseek.model' or uses 'deepseek-chat'.
    If ``env`` is provided (Odoo environment), auto-resolves key + model.
    """
    if env:
        if not api_key:
            api_key = get_api_key(env)
        if not model:
            model = get_model(env)

    if not api_key:
        raise ValueError("DeepSeek API key not configured (deepseek.api_key).")

    if not model:
        model = DEFAULT_MODEL

    all_messages = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": all_messages,
    }

    delay = BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)

            if resp.status_code not in RETRYABLE_STATUS:
                if not resp.ok:
                    # Log the response body so 400/401/etc. errors are debuggable
                    _logger.error(
                        "DeepSeek API %s: %s",
                        resp.status_code,
                        (resp.text or "")[:500],
                    )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

            if attempt == MAX_RETRIES:
                resp.raise_for_status()

            wait = min(float(resp.headers.get("Retry-After", delay)), MAX_DELAY)
            _logger.warning(
                "DeepSeek API %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, attempt + 1, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.Timeout as exc:
            if attempt == MAX_RETRIES:
                raise
            _logger.warning("DeepSeek API timeout (attempt %d/%d) — retrying in %.1fs",
                            attempt + 1, MAX_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.RequestException:
            raise

    return ""


# Alias for drop-in compatibility with code that imports 'openai_chat'
openai_chat = deepseek_chat
