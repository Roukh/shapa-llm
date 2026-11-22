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


# --- the personal-data gate (shapa/pii.py and hosted/src/pii.ts) -----------------------------------------

def test_the_pii_pattern_table_is_the_same_file_byte_for_byte():
    from shapa import piitable

    worker = (SRC / "pii-patterns.json").read_bytes()
    assert worker == piitable.PATH.read_bytes()
    assert json.loads(worker)["version"] == piitable.TABLE["version"]


def test_both_sides_read_the_table_they_hold_and_share_the_levels_and_stage_zero_tags():
    from shapa import pii

    for name in ("pii.ts", "redact.ts"):
        assert 'from "./pii-patterns.json"' in (SRC / name).read_text(encoding="utf-8"), name
    consts = _consts()
    assert (consts["FULL"], consts["OPERATOR"]) == (pii.FULL, pii.OPERATOR)
    assert _list("STAGE0_TAGS") == list(pii.STAGE0_TAGS) == ["stage:0", "stage-proposed:0"]


def test_the_rejection_sentence_is_the_same_on_both_sides():
    from shapa import pii

    ts = (SRC / "pii.ts").read_text(encoding="utf-8")
    head = "refused: the text looks like it holds "
    tail = ("Personal and sensitive data never goes into the wiki: remove it, or say what you mean without the value.")
    assert head in ts and tail in ts
    py = str(pii.Rejected(["email"]))
    assert py.startswith(head) and py.endswith(tail)
    assert "possible personal data (" in ts and "allowed, but check it is not about a person" in ts


def test_the_secret_patterns_of_the_table_are_masked_by_both_maskers():
    from shapa import piitable, redact

    names = [s["name"] for s in piitable.TABLE["secrets"]]
    assert [n for n, _ in redact.PATTERNS][-len(names):] == names
    ts = (SRC.parent / "test" / "redact.test.ts").read_text(encoding="utf-8")
    assert all(f'"{n}"' in ts for n in names)  # the vitest parity list names them in the same order



def test_the_skill_gate_sentence_is_the_same():
    worker = "".join(_literal(t) for t in re.findall(_STRING, _body_of("SKILL_GATE")))
    assert bootstrap.SKILL_GATE == worker
    assert len(worker) < 300 and "`skill_match`" in worker and worker.startswith("SKILL GATE: ")


def test_the_mempm_block_has_the_same_heading_keys_order_and_limits():
    consts = _consts()
    assert consts["MEMPM_HEAD"] == bootstrap.MEMPM_HEAD
    assert "[id, date, text]" in bootstrap.MEMPM_HEAD            # the field order of every entry
    assert _list("MEMPM_KEYS") == list(bootstrap.MEMPM_KEYS) == ["P", "X", "M"]
    worker = re.search(r"export const MEMPM_MAX = \{ P: (\d+), X: (\d+), M: (\d+) \}", _ts_files())
    assert worker and dict(zip("PXM", map(int, worker.groups()))) == bootstrap.MEMPM_MAX
    share = re.search(r"\bconst MEMPM_SHARE\s*=\s*([\d.]+)\s*;", _ts_files())
    assert share and float(share.group(1)) == bootstrap.MEMPM_SHARE
    assert _list("NOT_A_LESSON") == list(bootstrap.NOT_A_LESSON)
    assert consts["PRINCIPLE"] == "principle" and "principle" in db.LEARNING_TAGS


#: The entries both sides print for it, and the text they must make: also in hosted/test/instructions.test.ts.
MEMPM_SAMPLE = {"P": [["R1", "2026-11-20", 'say "hi" <b>&</b> x']], "X": [], "M": [["M2", "2026-11-19", 'a]],["b']]}
MEMPM_SAMPLE_JSON = ('{"P":[["R1","2026-11-20","say \\"hi\\" \\u003cb\\u003e\\u0026\\u003c/b\\u003e\\u2028x"]],"X":[],'
                     '"M":[["M2","2026-11-19","a]],[\\"b"]]}')


def test_the_mempm_json_is_written_alike_on_both_sides():
    """Compact JSON, keys P, X, M, the markup characters and line separators escaped. The Worker's vitest holds its
    `mempmJson` to the same expected text."""
    assert bootstrap.mempm_json(MEMPM_SAMPLE) == MEMPM_SAMPLE_JSON
    assert json.loads(MEMPM_SAMPLE_JSON) == MEMPM_SAMPLE


def test_the_reserved_document_tags_and_the_node_alias_prefix_match():
    assert _list("RESERVED_DOC_TAGS") == list(db.RESERVED_DOC_TAGS)
    consts = _consts()
    assert (consts["NODE_TAG"], consts["NODE_ALIAS_PREFIX"]) == (db.NODE_TAG, db.NODE_ALIAS_PREFIX)
    assert db.NODE_TAG in db.SYSTEM_TAGS and "shapa-node" == db.NODE_TAG


def test_the_atomic_rule_word_list_and_the_shared_cases_match_the_worker():
    ts = (SRC / "atomic.ts").read_text(encoding="utf-8")
    words = re.search(r"const COMMAND_WORD_LIST[^=]*=\s*\[(.*?)\];", ts, re.S).group(1)
    assert sorted(_literal(t) for t in re.findall(_STRING, words)) == sorted(db.COMMAND_WORDS)
    cases = json.loads((SRC.parent / "test" / "atomic-cases.json").read_text(encoding="utf-8"))
    assert cases["commands"] == sorted(db.COMMAND_WORDS)
    for c in cases["cases"]:
        assert db.several_rules(c["summary"], c["body"]) == c["why"], c["name"]


def test_the_hierarchy_line_the_name_patterns_and_the_node_path_cap_match():
    from shapa import hierarchy, node

    assert _consts()["HIERARCHY_PREFIX"] == hierarchy.PREFIX
    assert _number("MAX_SIBLINGS") == hierarchy.MAX_SIBLINGS and _number("MAX_PATH") == node.MAX_PATH
    ts = (SRC / "nodes.ts").read_text(encoding="utf-8")
    # Python spells the end of text `\Z`: its `$` would accept a trailing newline, which JavaScript's `$` does not
    assert re.search(r"const NAME_RE = /(.*)/;", ts).group(1) == hierarchy.NAME_RE.pattern.replace("\\Z", "$")
    assert re.search(r"const KEY_RE = /(.*)/;", ts).group(1) == hierarchy.KEY_RE.pattern.replace("\\Z", "$")
    # the same line from the same pieces, on both sides: hosted/test/hierarchy.test.ts holds the TypeScript twin
    assert hierarchy.line(["a", "b"], "me", "main", ["x", "y"]) == "HIERARCHY: a > b > me (type main); siblings: x, y"
    assert hierarchy.line([], "me", None, []) == ""
    assert hierarchy.line(["a"], "me", None, [f"s{i}" for i in range(12)]) == \
        "HIERARCHY: a > me; siblings: " + ", ".join(f"s{i}" for i in range(8)) + " (+4 more)"
