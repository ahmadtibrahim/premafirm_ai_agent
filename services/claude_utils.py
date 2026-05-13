"""
Anthropic Claude API helper — drop-in replacement for openai_utils.

Key differences from OpenAI that this module handles transparently:
  • Endpoint  : https://api.anthropic.com/v1/messages
  • Auth      : x-api-key header (not Authorization: Bearer)
  • Version   : anthropic-version: 2023-06-01 header required
  • System    : top-level "system" field, NOT inside messages[]
  • Images    : {"type":"image","source":{"type":"base64",...}}
                instead of {"type":"image_url","image_url":{"url":"data:..."}}
  • Response  : content[0].text  (not choices[0].message.content)

Retry behaviour (same as openai_utils):
  429 / 529  — rate-limited       → back-off and retry
  500 / 502 / 503 / 504 — server  → back-off and retry
  Up to MAX_RETRIES attempts, doubling delay each time.
  Respects retry-after header when present.
"""

import logging
import time

import requests

_logger = logging.getLogger(__name__)

CLAUDE_URL     = "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2023-06-01"
DEFAULT_MODEL  = "claude-haiku-4-5-20251001"

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
MAX_RETRIES = 4
BASE_DELAY  = 1.0
MAX_DELAY   = 60.0


# ── Image format conversion ────────────────────────────────────────────────

def _convert_content(content):
    """Convert OpenAI-style content (string or list of parts) → Anthropic format."""
    if isinstance(content, str):
        return content

    parts = []
    for item in content:
        t = item.get("type", "")
        if t == "text":
            parts.append({"type": "text", "text": item["text"]})
        elif t == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # data:<media_type>;base64,<data>
                header, b64_data = url.split(";base64,", 1)
                media_type = header.split("data:")[1]
                parts.append({
                    "type": "image",
                    "source": {
                        "type":       "base64",
                        "media_type": media_type,
                        "data":       b64_data,
                    },
                })
            # else: external URL — Anthropic supports url source too
            else:
                parts.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        # ignore unknown part types silently
    return parts


def _build_claude_payload(messages, system, max_tokens, model):
    """
    Convert an OpenAI-style messages list into an Anthropic API payload dict.

    - Extracts any {"role": "system"} message and merges it into `system`.
    - Converts image_url parts to Anthropic's image format.
    """
    claude_messages = []
    extra_system_parts = []

    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # Merge into system prompt
            text = content if isinstance(content, str) else " ".join(
                p["text"] for p in content if p.get("type") == "text"
            )
            extra_system_parts.append(text)
            continue

        claude_messages.append({"role": role, "content": _convert_content(content)})

    # Combine all system parts
    full_system = "\n\n".join(filter(None, [system or ""] + extra_system_parts)).strip() or None

    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   claude_messages,
    }
    if full_system:
        payload["system"] = full_system

    return payload


# ── Core API call with retry ───────────────────────────────────────────────

def claude_chat(
    messages,
    system=None,
    max_tokens=1024,
    api_key=None,
    model=DEFAULT_MODEL,
    timeout=60,
):
    """
    Call Anthropic Claude and return the response text.

    ``messages`` follows the OpenAI format so existing callers need minimal
    changes:
      [{"role": "system", "content": "..."},
       {"role": "user",   "content": "..."}]

    Vision content in OpenAI ``image_url`` format is auto-converted.

    Raises ``requests.HTTPError`` after all retries are exhausted,
    or ``ValueError`` if no API key is supplied.
    """
    if not api_key:
        raise ValueError("Anthropic API key not configured (anthropic.api_key).")

    headers = {
        "x-api-key":         api_key,
        "anthropic-version": CLAUDE_VERSION,
        "content-type":      "application/json",
    }
    payload = _build_claude_payload(messages, system, max_tokens, model)

    delay = BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=timeout)

            if resp.status_code not in RETRYABLE_STATUS:
                resp.raise_for_status()
                # Parse response
                data = resp.json()
                return data["content"][0]["text"].strip()

            # Retryable status
            if attempt == MAX_RETRIES:
                resp.raise_for_status()   # will raise HTTPError

            wait = min(float(resp.headers.get("retry-after", delay)), MAX_DELAY)
            _logger.warning(
                "Claude API %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, attempt + 1, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.Timeout:
            if attempt == MAX_RETRIES:
                raise
            _logger.warning("Claude API timeout (attempt %d/%d) — retrying in %.1fs",
                            attempt + 1, MAX_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        except requests.exceptions.RequestException:
            raise   # DNS / connection refused — don't retry
