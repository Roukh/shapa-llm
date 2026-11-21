"""The personal-data gate's one pattern table.

``shapa/assets/pii-patterns.json`` is the canonical copy and ``hosted/src/pii-patterns.json`` a
byte-for-byte twin (``tests/test_parity.py`` holds them equal), so the Worker and the local engine
screen with the same patterns. Python compiles every pattern with ``re.ASCII`` (the Worker uses the
``u`` flag, whose ``\\d \\w \\s \\b`` are ASCII too); a ``(?P<name>`` group is spelled ``(?<name>``
in TypeScript.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PATH = Path(__file__).resolve().parent / "assets" / "pii-patterns.json"
TABLE: dict = json.loads(PATH.read_text(encoding="utf-8"))


def flags_of(spec: str) -> int:
    return re.I if "i" in (spec or "") else 0


def compile_ascii(source: str, flags: str = "") -> re.Pattern:
    return re.compile(source, flags_of(flags) | re.ASCII)
