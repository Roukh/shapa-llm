"""The Worker (``hosted/src``, TypeScript) and the local engine hold the same constants. Each test reads
the Worker's value from the TypeScript source text and compares it with the Python one, so the two
cannot drift apart without a red test. Without ``hosted/src`` (an installed wheel) the module is
skipped, and the reason is printed."""

import json
import re
from pathlib import Path

import pytest

from shapa import bootstrap, db, projectfile

SRC = Path(__file__).resolve().parents[1] / "hosted" / "src"

pytestmark = pytest.mark.skipif(not SRC.is_dir(),
                                reason="hosted/src is absent (an installed wheel carries no Worker source)")

_STRING = r'"(?:[^"\\]|\\.)*"'


def _ts_files() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.ts")))


def _literal(token: str) -> str:
    return json.loads(token)


def _consts() -> dict[str, str]:
    """Every ``const NAME = "text";`` (a single string literal) of the Worker's source."""
    return {m.group(1): _literal(m.group(2))
            for m in re.finditer(rf"\bconst ([A-Z][A-Z0-9_]*)(?:: string)? = ({_STRING});", _ts_files())}


def _body_of(name: str) -> str:
    """The text between ``const NAME ... = `` and its ``;`` at the end of the statement."""
    m = re.search(rf"\bconst {name}\b[^=]*=\s*(.*?);\s*$", _ts_files(), re.S | re.M)
    assert m, f"{name} not found in hosted/src"
    return m.group(1)


def _number(name: str) -> int:
    m = re.search(rf"\bconst {name}\s*=\s*([\d_]+)\s*;", _ts_files())
    assert m, f"{name} not found in hosted/src"
    return int(m.group(1).replace("_", ""))


def _list(name: str, seen=()) -> list[str]:
    """A string array constant: literals, names of other string constants, and ``...OTHER_ARRAY`` spreads."""
    text = re.search(rf"\bconst {name}\b[^=]*=\s*\[(.*?)\]\s*(?:as const)?\s*;", _ts_files(), re.S)
    assert text, f"{name} not found in hosted/src"
    consts = _consts()
    out: list[str] = []
    for tok in re.finditer(rf"{_STRING}|\.\.\.[A-Z][A-Z0-9_]*|[A-Z][A-Z0-9_]*", text.group(1)):
        t = tok.group(0)
        if t.startswith('"'):
            out.append(_literal(t))
        elif t.startswith("..."):
            assert t[3:] not in seen, "spread cycle"
            out += _list(t[3:], (*seen, name))
        else:
            out.append(consts[t])
    return out


def test_the_baseline_budget_is_6000_characters_on_both_sides():
    assert _number("BASELINE_BUDGET") == db.BASELINE_BUDGET == 6000


def test_a_baseline_records_body_prints_at_most_1200_characters_on_both_sides():
    assert _number("BASELINE_BODY_MAX") == bootstrap.BASELINE_BODY_MAX == 1200


def test_a_project_item_is_at_most_500_characters_on_both_sides():
    assert _number("ITEM_MAX") == projectfile.ITEM_MAX == 500


def test_the_project_template_is_the_same_text_byte_for_byte():
    consts = _consts()
    elements = [consts[t] if not t.startswith('"') else _literal(t)
                for t in re.findall(rf"{_STRING}|[A-Z][A-Z0-9_]*", re.search(
                    r"export const PROJECT_TEMPLATE = \[(.*?)\]\.join\(\"\\n\"\);", _ts_files(), re.S).group(1))]
    worker = "\n".join(elements)
    assert worker.encode("utf-8") == projectfile.template().encode("utf-8")
    assert worker.endswith("1. \n") and "### Architecture:" in worker


def test_the_project_section_keys_match_in_order():
    assert _list("SECTIONS") == list(projectfile.SECTIONS)


def test_the_system_tags_and_tag_families_match_in_order():
    assert _list("SYSTEM_TAGS") == list(db.SYSTEM_TAGS)
    assert _list("SYSTEM_PREFIXES") == list(db.SYSTEM_TAG_PREFIXES)
    assert _list("LEARNING_TAGS") == list(db.LEARNING_TAGS)
    assert _list("LEARNING_PREFIXES") == list(db.LEARNING_TAG_PREFIXES)


def test_the_stage_limits_and_markers_match():
    assert _number("STAGE_MAX") == db.STAGE_MAX
    consts = _consts()
    assert (consts["STAGE_PREFIX"], consts["PROPOSED_PREFIX"]) == (db.STAGE_PREFIX, db.PROPOSED_PREFIX)
    assert consts["PROPOSED_TAG"] == db.STAGE_PROPOSED and consts["BASELINE_TAG"] == f"{db.STAGE_PREFIX}0"


def test_the_precedence_sentence_is_the_same():
    worker = "".join(_literal(t) for t in re.findall(_STRING, _body_of("PRECEDENCE")))
    assert bootstrap.PRECEDENCE == f"PRECEDENCE: {worker}"
    assert "these win" in worker
