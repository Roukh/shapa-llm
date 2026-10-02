"""Secret redaction - applied to every v3 memory record before it is hashed,
embedded, or written (shapa/memlog.py), whichever path produced it: the
Stop-hook capture, the optional LLM distiller, ``shapa upgrade``'s
conversion of legacy session notes, or the memri importer.

A fixed, stdlib-only regex set covering credential shapes that show up in
agent sessions: provider API keys, VCS/chat tokens, cloud access keys, JWTs,
PEM private keys, ``Bearer`` headers, passwords inside connection strings,
and ``KEY=value``-style assignments. A match is replaced with
:data:`PLACEHOLDER` (for patterns with a named ``secret`` group, only that
group is replaced so the surrounding context survives). This is a screen,
not a guarantee - it cannot know a secret that has no recognisable shape -
so capture also never stores the operator's raw first prompt or any tool
output (see shapa/capture.py).
"""

from __future__ import annotations

import re

PLACEHOLDER = "[REDACTED]"

#: (name, pattern). A pattern with a ``secret`` group masks only that group.
PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("pem-private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("stripe-webhook", re.compile(r"\bwhsec_[A-Za-z0-9]{10,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghs|ghr|ghu)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}")),
    ("supabase-key", re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]{10,}")),
    ("resend-key", re.compile(r"\bre_[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    ("url-password", re.compile(
        r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/?#@]+:(?P<secret>[^\s@/]{3,})@", re.I)),
    ("bearer", re.compile(r"\bBearer\s+(?P<secret>[A-Za-z0-9._~+/=-]{10,})")),
    ("basic-auth", re.compile(r"\bBasic\s+(?P<secret>[A-Za-z0-9+/=]{16,})")),
    # KEY=value / "token": "value" / password: value - the name must look
    # like a credential, and the value must be a single unbroken run (prose
    # such as "token: none of these apply" stays untouched).
    ("credential-assignment", re.compile(
        r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|pwd|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret)[\w.-]*[\"']?\s*[:=]\s*[\"']?"
        r"(?P<secret>[A-Za-z0-9+/_=.~-]{12,})")),
    ("conn-string-password", re.compile(r"(?i)\b(?:password|pwd)\s*=\s*(?P<secret>[^;\s\"']{6,})")),
)


def _mask(match: re.Match) -> str:
    if "secret" in match.re.groupindex and match.group("secret") is not None:
        start, end = match.span("secret")
        full_start = match.start()
        text = match.group(0)
        return text[: start - full_start] + PLACEHOLDER + text[end - full_start:]
    return PLACEHOLDER


#: A token that starts like a known key prefix but may have been cut by a
#: line wrap, and the whitespace-separated run that follows it.
_PREFIX_SPLIT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])((?:sk-|rk_|pk_|whsec_|gh[opsru]_|github_pat_|glpat-|xox[abposr]-|AKIA|ASIA|"
    r"AIza|hf_|npm_|sb_|re_|eyJ)[A-Za-z0-9_.-]*)(\s+)([A-Za-z0-9_+/=.-]{4,})")
#: Key-shaped runs right after a redaction - the rest of a secret that a
#: line wrap or a reflow split off (a run with a digit, or a long one).
_TAIL_RE = re.compile(
    re.escape(PLACEHOLDER) + r"(?:\s+(?:(?=[A-Za-z_+/=.-]*[0-9])[A-Za-z0-9_+/=.-]{6,}"
    r"|[A-Za-z0-9_+/=.-]{20,}))+")


def _matches_any(text: str) -> bool:
    return any(p.search(text) for _name, p in PATTERNS)


def _join_split_prefixes(text: str) -> str:
    """Re-join a key whose first piece is too short to match on its own
    (``sk-ant-api03-ab`` + newline + the rest): the pieces are joined only
    when the joined token matches a pattern the first piece alone does not."""
    for _ in range(4):
        def join(m: re.Match) -> str:
            head, tail = m.group(1), m.group(3)
            if not _matches_any(head) and _matches_any(head + tail):
                return head + tail
            return m.group(0)
        joined = _PREFIX_SPLIT_RE.sub(join, text)
        if joined == text:
            break
        text = joined
    return text


def scrub(text: str) -> tuple[str, int]:
    """Return ``(redacted_text, hits)``. Idempotent: the placeholder itself
    never matches any pattern. A secret split by whitespace (a wrapped or
    reflowed line) is caught both ways: a short first piece is re-joined
    before matching, and key-shaped pieces after a match are absorbed."""
    if not text:
        return text, 0
    hits = 0
    out = _join_split_prefixes(text)
    for _name, pattern in PATTERNS:
        out, n = pattern.subn(_mask, out)
        hits += n
    if hits:
        out = _TAIL_RE.sub(PLACEHOLDER, out)
    return out, hits


def redact(text: str) -> str:
    """:func:`scrub` without the hit count."""
    return scrub(text)[0]
