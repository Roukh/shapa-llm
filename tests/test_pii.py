"""The personal-data gate (shapa/pii.py): one fixture table shared with the Worker's vitest suite
(``hosted/test/pii-cases.json``), plus the evasion and speed checks that only make sense per language."""

import json
import time
from pathlib import Path

import pytest

from shapa import pii

CASES_PATH = Path(__file__).resolve().parents[1] / "hosted" / "test" / "pii-cases.json"
pytestmark = pytest.mark.skipif(not CASES_PATH.is_file(), reason="hosted/test/pii-cases.json is absent")
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"] if CASES_PATH.is_file() else []


def _fields(case):
    return ["".join(f) if isinstance(f, list) else f for f in case["fields"]]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_shared_fixture(case):
    found = pii.scan(_fields(case), case["level"])
    assert sorted(found.blocked) == case["blocked"]
    if not case["blocked"]:
        assert sorted(found.warned) == case["warned"]


def test_a_rejection_names_the_category_and_never_the_value():
    with pytest.raises(pii.Rejected) as err:
        pii.check(["write to john.doe@gmail.com or call 415-555-2671"])
    assert "email" in str(err.value) and "phone" in str(err.value)
    assert "john" not in str(err.value) and "415" not in str(err.value)
    assert err.value.categories == ["email", "phone"]


def test_a_clean_write_returns_its_warnings_without_the_value():
    out = pii.check(["server at 93.184.216.34"])
    assert len(out) == 1 and "93.184" not in out[0] and "IPv4" in out[0]
    assert pii.check(["nothing personal here"]) == []


def test_stage_zero_escalates_the_privileged_writer_to_every_category():
    assert pii.level_for(["stage:0"], privileged=True) == pii.FULL
    assert pii.level_for([" Stage-Proposed:0 "], privileged=True) == pii.FULL
    assert pii.level_for(["stage:2"], privileged=True) == pii.OPERATOR
    assert pii.level_for([], privileged=False) == pii.FULL
    with pytest.raises(pii.Rejected):
        pii.check(["john@gmail.com"], pii.level_for(["stage:0"], True))
    assert pii.check(["john@gmail.com"], pii.level_for(["stage:2"], True)) == []


@pytest.mark.parametrize("name,text", [
    ("overlong digit run", "7" * 100_000),
    ("spaced digits", "4 " * 50_000),
    ("hyphen digit runs", "12-" * 40_000),
    ("local part run", "a" * 100_000),
    ("at signs", "a@" * 50_000),
    ("domain labels", "x@" + "a." * 50_000),
    ("capitalised words", "1 " + "Aaaa " * 20_000 + "Street"),
    ("street numbers", "1 A " * 25_000),
    ("sentence dots", "dob " + "1/" * 50_000),
    ("keyword spam", "passport " * 10_000),
    ("pin spam", "pin: " * 20_000),
    ("pem header", "-----BEGIN " + "A " * 40_000),
    ("coordinates", "1.2345," * 15_000),
    ("base64 soup", "QUJD" * 25_000),
    ("entity soup", "&#64;" * 20_000),
    ("health line", "my " * 30_000 + "cancer"),
])
def test_no_adversarial_input_makes_the_gate_slow(name, text):
    # the whole gate over 100 KB of the worst input of each kind: well under a second
    t0 = time.perf_counter()
    pii.scan([text])
    assert time.perf_counter() - t0 < 1.0, name


def test_a_dump_of_card_shaped_digit_groups_is_refused_not_ground_through():
    # 50,000 candidate windows: past the candidate budget the text is refused whole, in linear time
    t0 = time.perf_counter()
    assert pii.scan(["4 " * 50_000]).blocked == ["card"]
    assert time.perf_counter() - t0 < 0.5
    assert pii.scan(["1 " * 50_000]).blocked == []  # digits no card network starts with are not candidates


def test_the_masker_is_linear_on_dotted_and_hyphenated_runs():
    # `credential-assignment` used to rescan 64 characters from every boundary of such a run (300+ ms at 100 KB)
    from shapa import redact

    for text in ("12-" * 40_000, "x@" + "a." * 50_000, "a.b-" * 25_000):
        t0 = time.perf_counter()
        redact.scrub(text)
        assert time.perf_counter() - t0 < 0.15
    assert redact.scrub("api_key = abcdefghijklmnop1234")[0] == "api_key = [REDACTED]"
    assert redact.scrub("my-very-long-name-" * 8 + "token=abcdefghijklmnop1234")[0].endswith("token=[REDACTED]")


def test_every_pattern_scans_100kb_in_under_50ms():
    junk = ("lorem ipsum 12 dolor sit amet, consectetur 3 adipiscing elit 4111 sed do eiusmod "
            "tempor@ incididunt ut labore 1.2.3.4 et dolore magna Main St. aliqua. ") * 700
    junk = junk[:100_000]
    view = pii.normalize(junk)
    slow = []
    for cat in (*pii.compiled()[0], *pii.compiled()[1]):
        if cat.name == "secret":
            continue
        t0 = time.perf_counter()
        pii._category_hit(cat, view)
        ms = (time.perf_counter() - t0) * 1000
        if ms > 50:
            slow.append((cat.name, round(ms, 1)))
    assert not slow, slow
