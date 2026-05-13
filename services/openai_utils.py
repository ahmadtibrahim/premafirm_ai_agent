"""
Shared OpenAI HTTP helper with exponential-backoff retry.

Retries on:
  429 Too Many Requests  — rate limit / quota exceeded
  500 / 502 / 503 / 504  — transient server errors

Back-off schedule (configurable via MAX_RETRIES / BASE_DELAY):
  attempt 1 →  1 s
  attempt 2 →  2 s
  attempt 3 →  4 s
  then raises the last exception

Respects the Retry-After header when the server sends one.
"""

import logging
import time

import requests

_logger = logging.getLogger(__name__)

OPENAI_CHAT_URL  = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL    = "gpt-4o-mini"   # language/vision tasks — cheapest reliable
DEFAULT_MODEL_NANO = "gpt-4.1-nano"  # extraction/classification — cheapest text

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0        # seconds — doubles each retry
MAX_DELAY  = 60.0       # cap so Retry-After headers can't stall forever


def openai_chat(
    messages,
    system=None,
    max_tokens=1024,
    api_key=None,
    model=DEFAULT_MODEL,
    timeout=60,
):
    """
    Call OpenAI chat completions and return response text.
    Drop-in replacement for claude_chat() — same parameter signature.

    ``messages`` follows OpenAI format:
      [{"role": "user", "content": "..."}, ...]
    Vision content in OpenAI image_url format is passed through unchanged.
    """
    if not api_key:
        raise ValueError("OpenAI API key not configured (openai.api_key).")

    all_messages = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   all_messages,
    }

    resp = openai_post(OPENAI_CHAT_URL, headers, json=payload, timeout=timeout)
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def openai_post(url, headers, payload=None, timeout=60, *, json=None):
    """POST to an OpenAI-compatible endpoint with automatic retry.

    Returns a ``requests.Response`` with status 2xx.
    Raises ``requests.HTTPError`` after all retries are exhausted.
    """
    body = json if json is not None else payload  # accept either kwarg name

    last_exc = None
    delay = BASE_DELAY

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)

            if resp.status_code not in RETRYABLE_STATUS:
                resp.raise_for_status()   # raises immediately on 4xx/5xx non-retryable
                return resp

            # --- retryable status ---
            if attempt == MAX_RETRIES:
                resp.raise_for_status()   # exhausted — let caller handle

            wait = min(
                float(resp.headers.get("Retry-After", delay)),
                MAX_DELAY,
            )
            _logger.warning(
                "OpenAI %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, attempt + 1, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise
            _logger.warning("OpenAI timeout (attempt %d/%d) — retrying in %.1fs",
                            attempt + 1, MAX_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.RequestException as exc:
            raise   # non-retryable network error (DNS, connection refused, etc.)

    if last_exc:
        raise last_exc
