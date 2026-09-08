"""
Customer-email text sanitizer (CRM Preliminary Estimate workflow).

Fact extraction must read the GENUINE live request only — email bodies as
stored by Odoo are full threads: the live reply, then the quoted history of
earlier messages, the sender's signature and the confidentiality footer.
None of that is shipment data; quoted history especially can silently
re-extract superseded facts (the supersession ordering then has to fight
stale text).  This module strips, in order:

1. HTML: everything from the first <blockquote> on (quoted history).
2. Plain text:
   - reply-quote boundaries  ("On Tue, Sep 8 2026 at 9:03 AM J <j@x> wrote:"),
   - '>' -prefixed quote runs (2+ consecutive lines),
   - '-----Original Message-----'-style dividers that introduce a header
     block (From:/Sent:/To:/Subject:/Date:) — the earlier thread,
   - RFC 3676 signature delimiters ('--' alone on a line),
   - and from the TAIL only: footer boilerplate lines (confidentiality /
     virus-scan notices, "Sent from my iPhone") that follow the content.
3. Stray e-mail header lines that survived the above.

Forwards are the one case where the quoted part IS the content: a
'Forwarded message' divider is removed together with the header block that
follows it, and everything else is kept.

Design rules
------------
* Purely functional — no ORM, no AI, no I/O.  Sanitizers compose; callers
  decide where to apply them (LeadFactService applies them to every
  customer document it gathers).
* When in doubt, KEEP text: the pipeline only ever cuts at a recognizable
  boundary or from the tail.  A divider with no header block behind it is
  left alone.  The downstream prompt also tells the LLM to ignore
  signatures/disclaimers/history, so this is defence in depth, not the
  single gate.
"""

import logging
import re

_logger = logging.getLogger(__name__)

# ── HTML cut: quoted history sits in <blockquote> elements ────────────────
_BLOCKQUOTE_RE = re.compile(r"<blockquote\b", re.IGNORECASE)

# ── plain-text reply-quote boundary ("On <date> <person> wrote:") ─────────
# A line is treated as an attribution only when it ALSO carries a date hint
# (month/weekday name, year, day number, clock time or an address token) —
# content sentences like "On the rate card we wrote:" are never cut.
_DATE_HINT_RE = re.compile(
    r"\b(?:20\d\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|\d{1,2}(?:st|nd|rd|th)?|"
    r"\d{1,2}:\d{2})\b|@",
    re.IGNORECASE,
)
_WROTE_RE = re.compile(
    r"^\s*(?:>+\s*)?(?:on|le|el|em|am|il|den|op)\b[^\n]{0,160}?"
    r"\b(?:wrote(?:\s+the\s+following)?|a\s+[ée]crit|escrib[ióo]|schrieb|"
    r"skrev|scris|kirjoitti)\b:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ── divider / thread-boundary lines ───────────────────────────────────────
_DIVIDER_RE = re.compile(
    r"^\s*[-_=*#]{8,}\s*$")                       # "---...", "____..."
_DIVIDER_LABEL_RE = re.compile(
    r"^\s*(?:[-_=*#]+\s*)?(?:forwarded message|original message|"
    r"message transf[ée]r[ée]|message d'origine|"
    r"weitergeleitete nachricht|mensaje reenviado|"
    r"mensagem encaminhada|urspr[üu]ngliche nachricht)\s*[-_=*#]*:?\s*$",
    re.IGNORECASE,
)

# E-mail header labels that introduce a quoted/forwarded block.
_HEADER_RE = re.compile(
    r"^(?:from|to|cc|bcc|subject|date|sent|reply-to|sender|importance|"
    r"received|mime-version|content-type|content-transfer-encoding|x-[a-z0-9-]+)"
    r"(\s)*:",
    re.IGNORECASE,
)
_HEADER_LABELS_ANYWHERE = re.compile(
    r"^\s*(?:from|to|cc|bcc|subject|date|sent|sender|reply-to|importance|"
    r"mime-version|content-type|content-transfer-encoding|received):\s*",
    re.IGNORECASE,
)

# ── footer / signature boilerplate (stripped from the TAIL only) ──────────
# Strong markers only ever appear in boilerplate; weak ones ("confidential",
# "this email...") can appear inside real content, so a weak-only line is
# dropped only once the tail strip is already inside a footer block.
_FOOTER_STRONG_RE = re.compile(
    r"(?:"
    r"privileged|intended recipient|solely for the use|virus|antivirus|"
    r"electronic (?:communication|message)|please consider the environment|"
    r"sent from (?:my )?(?:iphone|ipad|android|blackberry|mobile)|"
    r"(?:get )?outlook for|not (?:intended|directed) for|"
    r"disseminat\w+|not be disclosed|scan(?:ning)? (?:for|of) (?:viruses|"
    r"content|malware)"
    r")",
    re.IGNORECASE,
)
_FOOTER_WEAK_RE = re.compile(
    r"(?:"
    r"confiden\w*|may contain\w*|"
    r"(?:this|the) (?:e-?mail|message).{0,60}(?:contains|including)|"
    r"not (?:intended|directed) to|unauthori[sz]ed"
    r")",
    re.IGNORECASE,
)

# Openers that identify a boilerplate/footer line as such (used to accept a
# single-marker line as the OUTERMOST footer line).
_FOOTER_OPENER_RE = re.compile(
    r"^(?:"
    r"this\s+(?:e-?mail|message|communication)\b|"
    r"the\s+(?:information|contents?|material)\b|"
    r"please\s+consider\b|sent\s+from\b|if\s+you\s+are\s+not\b|"
    r"any\s+attachments?\b|confidentiality\b|legal\s+notice\b|"
    r"(?:e-?mail|message)\s+(?:is|contains|may)\b"
    r")",
    re.IGNORECASE,
)


def _is_quote_run(lines, i):
    """True when line ``i`` starts a run of quoted ('>') lines worth cutting.

    Cut when the run is >=2 lines and there is live content ABOVE it, or the
    run is >=3 lines even at the very top (a top-posted reply can carry a
    short leading quote before its own text; a pure-quote auto-reply has a
    long run)."""
    if not lines[i].lstrip().startswith(">"):
        return False
    run = 0
    j = i
    while j < len(lines) and lines[j].lstrip().startswith(">"):
        run += 1
        j += 1
    if run >= 3:
        return True
    return run >= 2 and i > 0


def _header_block(lines, i, max_ahead=8):
    """When ``lines[i]`` starts a quoted/forwarded header block, return the
    index AFTER the header lines (the divider + headers are consumed).
    A block = a divider/label line followed by >=1 header line (>=2 header
    lines when the divider is bare), within ``max_ahead`` lines."""
    s = lines[i].strip()
    labeled = bool(_DIVIDER_LABEL_RE.match(s))
    bare = bool(_DIVIDER_RE.match(s))
    if not (labeled or bare):
        return None
    headers = 0
    j = i + 1
    while j < len(lines) and j <= i + max_ahead:
        line = lines[j].strip()
        if not line:
            j += 1
            continue
        if _HEADER_RE.match(line):
            headers += 1
            j += 1
            if labeled:
                continue  # consume every consecutive header line
            if headers >= 2:
                return j
            continue
        break  # content line — not a header block
    return j if (labeled and headers >= 1) else None


def sanitize_email_text(text, max_chars=20000):
    """Return the sanitized plain text of ONE customer message.

    ``text`` — the plain text of a single e-mail (already converted from
    HTML).  Quoted history, signatures and footers are removed as described
    in the module docstring.  Empty result means nothing recognizable as
    live content remains (a pure quote/auto-reply).
    """
    text = (text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        # RFC 3676 signature delimiter: everything below is the signature.
        if s == "--" or s.startswith("-- "):
            break
        # Reply-quote boundary ("On ..., X wrote:") → earlier thread history.
        if _WROTE_RE.match(s) and _DATE_HINT_RE.search(s):
            break
        # '>' -prefixed quote run.
        if _is_quote_run(lines, i):
            break
        # Divider-led block.  Forwards keep their content (only the divider
        # + header lines are dropped); every other block is earlier history.
        if _DIVIDER_LABEL_RE.match(s) or _DIVIDER_RE.match(s):
            after = _header_block(lines, i)
            if after is None:
                out.append(lines[i])  # divider with no headers: keep
            elif "forward" in s.lower():
                i = after - 1         # skip divider + headers, keep the body
            else:
                break                 # 'Original message' / bare divider: cut
        else:
            out.append(lines[i])
        i += 1
    if not out:
        return ""
    # Drop stray header lines that survived (never drops 'ref:'-style lines).
    kept = [ln for ln in out
            if not _HEADER_LABELS_ANYWHERE.match(ln) or len(ln.strip()) > 500]
    # Tail strip: footer boilerplate / device sign-off lines only.
    # The OUTERMOST footer line must be unambiguous — a boilerplate opener,
    # or >=2 distinct markers — so content that merely contains a marker
    # word ("confidential pricing", "not directed for...") is never eaten.
    # Once inside a footer block, every following marker line is popped.
    in_footer = False
    while kept:
        s = kept[-1].strip()
        if not s:
            kept.pop()
            continue
        strong_hits = len(_FOOTER_STRONG_RE.findall(s))
        weak_hits = len(_FOOTER_WEAK_RE.findall(s))
        if not strong_hits and not weak_hits:
            break
        if in_footer:
            kept.pop()
            continue
        if strong_hits + weak_hits >= 2 or (
                strong_hits and _FOOTER_OPENER_RE.search(s)):
            in_footer = True
            kept.pop()
        else:
            break
    result = "\n".join(kept)
    # Squeeze 3+ blank lines and trim.
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result[:max_chars]


def sanitize_email_body_html(html, max_chars=20000):
    """Sanitize a stored mail.message ``body`` (HTML) down to live text.

    The HTML quote cut runs FIRST (blockquote subtrees are the whole
    earlier thread, and html2plaintext would otherwise flatten their text
    with no reliable marker left).
    """
    if not html:
        return ""
    m = _BLOCKQUOTE_RE.search(html)
    if m:
        html = html[: m.start()]
    from odoo.tools import html2plaintext  # noqa: PLC0415 (lazy: keeps this
    # module importable without a full Odoo runtime for pure unit tests)
    plain = html2plaintext(html or "")
    return sanitize_email_text(plain, max_chars=max_chars)
