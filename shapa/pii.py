"""The personal-data gate: personal and sensitive information never enters stage 0, and no AI app
(an MCP tool, a hook, a sync push: every writer that is not the operator's own CLI) puts it into any wiki.

:mod:`shapa.redact` only MASKS secrets. This module REFUSES a write: :func:`check` raises
:class:`Rejected` naming the categories found (never the matched value) and returns the warnings of a
write it lets through (a public IP address, a MAC address, a wallet address, a postcode, "the user's ...
is" phrasing, a named relative: allowed, counted, never echoed).

One pattern table, ``shapa/assets/pii-patterns.json`` (see :mod:`shapa.piitable`), is shared with the
Worker (``hosted/src/pii.ts``, a line-for-line twin held equal by ``tests/test_parity.py`` and a shared
fixture file). Two levels:

* ``FULL`` - every category: the unprivileged writers, and ANY write that targets stage 0
  (``stage:0`` or ``stage-proposed:0``) whoever writes it;
* ``OPERATOR`` - the privileged local CLI: only the categories marked ``operator`` (card, IBAN, SSN,
  private keys, government IDs, dates of birth, first-person health data).

Before matching, the text is normalised so an encoding trick does not hide a value: invisible and
zero-width characters stripped, NFKC (full-width forms, ligatures), non-ASCII digits mapped to ASCII,
combining marks dropped, Unicode spaces folded. The decoded forms are scanned too: HTML numeric and
common named entities, ``%XX`` and ``\\uXXXX`` escapes, ``[at]``/``[dot]`` spelling, and base64 runs
that decode to text. Every field of a write is scanned alone-joined by a newline and joined by a space,
so a value split across a summary and a body is still one value.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from shapa import redact
from shapa.piitable import TABLE, compile_ascii

FULL = "full"
OPERATOR = "operator"
STAGE0_TAGS = ("stage:0", "stage-proposed:0")

_N = TABLE["normalize"]
_INVISIBLE = re.compile("[" + _N["invisible"] + "]")
_SPACES = re.compile("[" + _N["spaces"] + "]")
_DASHES = re.compile("[" + _N["dashes"] + "]")
_DOTS = re.compile("[" + _N["dots"] + "]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]+")
_ENTITY = re.compile(r"&(?:#(\d{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z]{2,8}));?")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})")
_AT = re.compile(r"\s*[\[({]\s*at\s*[\])}]\s*", re.I)
_DOT = re.compile(r"\s*[\[({]\s*dot\s*[\])}]\s*", re.I)
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{%d,}={0,2}" % _N["limits"]["base64_min"])
_HEX_RUN = re.compile(r"(?:[0-9A-Fa-f]{2}){8,}")
_STOP = frozenset(TABLE["street_stop"])


def _fold_non_ascii(chunk: str) -> str:
    out = []
    for ch in chunk:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        if cat == "Nd":
            out.append(str(unicodedata.decimal(ch)))
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str) -> str:
    """The text as the patterns see it (see the module docstring). Pure ASCII skips the Unicode steps."""
    if not text.isascii():
        text = _INVISIBLE.sub("", text)
        text = unicodedata.normalize("NFKC", text)
        text = _INVISIBLE.sub("", text)
        text = _SPACES.sub(" ", text)
        text = _DASHES.sub("-", text)
        text = _DOTS.sub(".", text)
        text = _NON_ASCII.sub(lambda m: _fold_non_ascii(m.group(0)), text)
    return _CONTROL.sub("", text)


def _entity(m: re.Match) -> str:
    try:
        if m.group(1) is not None:
            return chr(int(m.group(1)))
        if m.group(2) is not None:
            return chr(int(m.group(2), 16))
    except (ValueError, OverflowError):
        return m.group(0)
    return _N["entities"].get(m.group(3).lower(), m.group(0))


def _decode_layer(text: str) -> str:
    if "&" in text:
        text = _ENTITY.sub(_entity, text)
    if "%" in text:
        text = _PERCENT.sub(lambda m: chr(int(m.group(1), 16)) if 0x20 <= int(m.group(1), 16) < 0x7f else m.group(0), text)
    if "\\" in text:
        text = _ESCAPE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
    return text


def _printable_text(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 6:
        return None
    good = sum(1 for c in text if c.isprintable() or c in "\n\t\r")
    return text if good / len(text) >= 0.95 else None


def _base64_texts(text: str) -> list[str]:
    out: list[str] = []
    for m in list(_B64_RUN.finditer(text))[: _N["limits"]["base64_runs"]]:
        run = m.group(0).rstrip("=")
        run = run.replace("-", "+").replace("_", "/")
        run += "=" * (-len(run) % 4)
        try:
            decoded = base64.b64decode(run, validate=True)
        except (binascii.Error, ValueError):
            continue
        shown = _printable_text(decoded)
        if shown:
            out.append(shown)
    for m in list(_HEX_RUN.finditer(text))[: _N["limits"]["base64_runs"]]:
        shown = _printable_text(bytes.fromhex(m.group(0)))
        if shown:
            out.append(shown)
    return out


def _views(text: str, depth: int = 0) -> list[str]:
    """The normalised text and every decoded form of it (entities, escapes, ``[at]``, base64)."""
    base = normalize(text)
    seen = [base]

    def add(v: str) -> None:
        if v and v not in seen:
            seen.append(v)

    cur = base
    for _ in range(_N["limits"]["decode_rounds"]):
        nxt = normalize(_decode_layer(cur))
        if nxt == cur:
            break
        cur = nxt
        add(cur)
    for v in list(seen):
        if "[" in v or "(" in v or "{" in v:
            spelled = _DOT.sub(".", _AT.sub("@", v))
            if spelled != v:
                add(spelled)
    if depth < 2:
        for decoded in _base64_texts(base):
            for v in _views(decoded, depth + 1):
                add(v)
    return seen


# --- validators ------------------------------------------------------------------------------------

def _luhn_ok(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    first = digits[0]
    if not (first in "3456" or (first == "2" and 2221 <= int(digits[:4]) <= 2720)):
        return False  # no card network issues a number that starts like this: a timestamp, an id
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    s = re.sub(r"[ .-]", "", text)
    if not 15 <= len(s) <= 34:
        return False
    r = 0
    for ch in s[4:] + s[:4]:
        v = ord(ch) - 48 if ch.isdigit() else ord(ch) - 55
        r = (r * 10 + v) % 97 if v < 10 else (r * 100 + v) % 97
    return r == 1


_NOT_GLOBAL = (
    ((0, 0, 0, 0), 8), ((10, 0, 0, 0), 8), ((100, 64, 0, 0), 10), ((127, 0, 0, 0), 8), ((169, 254, 0, 0), 16),
    ((172, 16, 0, 0), 12), ((192, 0, 0, 0), 24), ((192, 0, 2, 0), 24), ((192, 88, 99, 0), 24), ((192, 168, 0, 0), 16),
    ((198, 18, 0, 0), 15), ((198, 51, 100, 0), 24), ((203, 0, 113, 0), 24), ((224, 0, 0, 0), 4), ((240, 0, 0, 0), 4),
)


def _public_ipv4(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4 or any(len(p) > 1 and p[0] == "0" for p in parts):
        return False
    octets = [int(p) for p in parts]
    if any(o > 255 for o in octets) or max(octets) <= 20:  # all small: a version number, not a host
        return False
    n = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    for base, bits in _NOT_GLOBAL:
        b = (base[0] << 24) | (base[1] << 16) | (base[2] << 8) | base[3]
        if (n >> (32 - bits)) == (b >> (32 - bits)):
            return False
    return True


def _geo_ok(text: str) -> bool:
    lat, lon = text.split(",", 1)
    return not (abs(int(float(lat))) == 0 and abs(int(float(lon))) == 0)  # "0.1234, 0.5678" is a pair of floats


def _street_ok(text: str, start: int) -> bool:
    m = re.search(r"([A-Za-z]+)\W*$", text[max(0, start - 24):start], re.ASCII)
    return not (m and m.group(1).lower() in _STOP)


def _valid(check: str | None, m: re.Match, text: str) -> bool:
    if check is None:
        return True
    if check == "luhn":
        return _luhn_ok(re.sub(r"\D", "", m.group(0)))
    if check == "iban":
        return _iban_ok(m.group(0))
    if check == "gov-digits":
        return len(re.sub(r"\D", "", m.group(1))) >= 5
    if check == "geo":
        return _geo_ok(m.group(0))
    if check == "street":
        return _street_ok(text, m.start())
    if check == "public-ipv4":
        return _public_ipv4(m.group(0))
    raise ValueError(f"unknown check {check!r}")


# --- the compiled table ----------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pat:
    rx: re.Pattern
    check: str | None
    allow: tuple[re.Pattern, ...]
    has_v: bool


@dataclass(frozen=True)
class _Cat:
    name: str
    label: str
    operator: bool
    pats: tuple[_Pat, ...] = ()
    pairs: tuple[tuple[re.Pattern, re.Pattern], ...] = ()


def _build(entry: dict, warn: bool = False) -> _Cat:
    pats = tuple(
        _Pat(compile_ascii(p["re"], p.get("flags", "")), p.get("check"),
             tuple(compile_ascii(a, p.get("allow_flags", "")) for a in p.get("allow", ())),
             "(?P<v>" in p["re"])
        for p in entry.get("patterns", ()))
    pairs = tuple((compile_ascii(p["marker"], p.get("flags", "")), compile_ascii(p["terms"], p.get("flags", "")))
                  for p in entry.get("pairs", ()))
    return _Cat(entry["name"], entry["label"], bool(entry.get("operator")), pats, pairs)


LABELS = {e["name"]: e["label"] for e in (*TABLE["categories"], *TABLE["warn"])}


@lru_cache(maxsize=1)
def compiled() -> tuple[tuple[_Cat, ...], tuple[_Cat, ...]]:
    """``(blocking categories, warning categories)``, compiled on first use: importing this module (every
    ``shapa`` command imports the database layer) must not pay for ~45 regular expressions."""
    return tuple(_build(e) for e in TABLE["categories"]), tuple(_build(e, True) for e in TABLE["warn"])


#: A text with more candidate matches than this for one pattern is a dump of card-shaped digit groups (or of
#: allowed addresses), not a note: refused outright, which also keeps the gate linear on hostile input.
MAX_CANDIDATES = 4000


def _hit(pat: _Pat, text: str) -> bool:
    pos = 0
    tried = 0
    while True:
        m = pat.rx.search(text, pos)
        if m is None:
            return False
        tried += 1
        if tried > MAX_CANDIDATES and (pat.check or pat.allow):
            return True
        if pat.allow:
            value = m.group("v") if pat.has_v else m.group(0)
            if any(a.search(value) for a in pat.allow):
                pos = m.end()  # an allowed value is consumed whole: its tail is not another candidate
                continue
        if _valid(pat.check, m, text):
            return True
        pos = m.start() + 1
        if pos >= len(text):
            return False


def _category_hit(cat: _Cat, view: str) -> bool:
    if any(_hit(p, view) for p in cat.pats):
        return True
    for marker, terms in cat.pairs:
        for line in view.split("\n"):
            if marker.search(line) and terms.search(line):
                return True
    return False


# --- the gate --------------------------------------------------------------------------------------

@dataclass
class Scan:
    blocked: list[str] = field(default_factory=list)
    warned: dict[str, int] = field(default_factory=dict)


def _fields_views(texts) -> tuple[list[str], list[str]]:
    fields = [str(t) for t in texts if t]
    if not fields:
        return [], []
    newline = "\n".join(fields)
    # a value split across two fields: joined by a space, and run together
    extra = [" ".join(fields), "".join(fields)] if len(fields) > 1 else []
    seen: list[str] = []
    for v in _views(newline) + [w for j in extra for w in _views(j)]:
        if v not in seen:
            seen.append(v)
    return seen, _views(newline)


def scan(texts, level: str = FULL) -> Scan:
    """What the gate finds in *texts* (the fields of one write) at *level*. Never returns a value."""
    views, line_views = _fields_views(texts)
    out = Scan()
    categories, warnings = compiled()
    for cat in categories:
        if level == OPERATOR and not cat.operator:
            continue
        pool = line_views if cat.pairs else views
        if cat.name == "secret":
            if any(redact.scrub(v)[1] for v in pool):
                out.blocked.append(cat.name)
            continue
        if any(_category_hit(cat, v) for v in pool):
            out.blocked.append(cat.name)
    if not out.blocked:
        for cat in warnings:
            n = sum(1 for v in line_views if _category_hit(cat, v))
            if n:
                out.warned[cat.name] = n
    return out


class Rejected(ValueError):
    """A write refused for personal or sensitive data. ``categories`` are names, never values."""

    def __init__(self, categories: list[str]):
        self.categories = list(categories)
        labels = ", ".join(LABELS.get(c, c) for c in self.categories)
        super().__init__(
            f"refused: the text looks like it holds {labels}. Personal and sensitive data never goes into "
            "the wiki: remove it, or say what you mean without the value.")


def warnings_of(found: Scan) -> list[str]:
    return [f"possible personal data ({LABELS.get(name, name)}): allowed, but check it is not about a person"
            for name in found.warned]


def check(texts, level: str = FULL) -> list[str]:
    """Raise :class:`Rejected` when *texts* hold blocked data at *level*; else the warnings (possibly none)."""
    found = scan(texts, level)
    if found.blocked:
        raise Rejected(found.blocked)
    if found.warned:
        import logging  # imported here: ``logging`` costs tens of milliseconds at import, on every command

        logging.getLogger("shapa.pii").info("pii-warn count=%d", sum(found.warned.values()))
    return warnings_of(found)


def scan_wiki(root, note_paths=()) -> list[tuple[str, list[str]]]:
    """``(record id, categories)`` of every stored record that an unprivileged writer would be refused today
    (``shapa validate --pii``). Reads only: nothing is rewritten, and no value is returned. Notes are files
    (*note_paths*); everything else is a row of the wiki's database."""
    from pathlib import Path

    from shapa import db

    found: list[tuple[str, list[str]]] = []
    conn = db.connect(root)
    if conn is not None:
        try:
            tags: dict[str, list[str]] = {}
            for rid, tag in conn.execute("SELECT id, tag FROM tags"):
                tags.setdefault(rid, []).append(tag)
            queries = (("SELECT id, summary, body, alias FROM mri", "id"), ("SELECT id, title, body, alias FROM docs", "id"),
                       ("SELECT id, title, body, alias FROM items", "id"))
            for sql, _key in queries:
                for rid, a, b, alias in conn.execute(sql):
                    hit = scan([a, b, alias, *tags.get(rid, ())], FULL).blocked
                    if hit:
                        found.append((rid, hit))
        finally:
            conn.close()
    for path in note_paths:
        try:
            hit = scan([Path(path).name, Path(path).read_text(encoding="utf-8", errors="replace")], FULL).blocked
        except OSError:
            continue
        if hit:
            found.append((str(path), hit))
    return found


def is_stage0(tags) -> bool:
    return any(" ".join(str(t).split()).lower() in STAGE0_TAGS for t in (tags or ()))


def level_for(tags, privileged: bool) -> str:
    """``FULL`` for every writer but the operator's CLI, and for ANY write that goes to stage 0."""
    return OPERATOR if privileged and not is_stage0(tags) else FULL
