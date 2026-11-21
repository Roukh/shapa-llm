"""The repo is public: tests and fixtures carry synthetic identities only (example domains, /home/user)."""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HOME = re.compile(r"/(?:home|Users)/([A-Za-z0-9._-]+)")
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com", "github.com")
SKIP = {"test_public_boundary.py", "test_pii.py", "test_pii_writes.py", "test_redact.py", "test_learn_import.py"}  # they hold the detector's samples


def files():
    for p in sorted(TESTS.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".md", ".json", ".txt", ".toml") and p.name not in SKIP \
                and "__pycache__" not in p.parts:
            yield p


def test_no_real_email_address_or_home_directory_in_tests_and_fixtures():
    bad = []
    for p in files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in EMAIL.finditer(text):
            domain = m.group(0).split("@", 1)[1].lower()
            if domain not in ALLOWED_DOMAINS and not domain.endswith((".example", ".example.com")) and not domain.startswith("localhost"):
                bad.append(f"{p.relative_to(TESTS)}: {m.group(0)}")
        for m in HOME.finditer(text):
            if m.group(1) not in ("user", "runner", "me", "x", "nobody", "name", "you", "u"):
                bad.append(f"{p.relative_to(TESTS)}: {m.group(0)}")
    assert not bad, "\n".join(bad[:20])


def test_the_operator_addresses_that_were_in_the_retrieval_fixture_are_gone():
    text = (TESTS / "fixtures/retrieval_eval/wiki/operator-identity.md").read_text(encoding="utf-8")
    assert "user@example.com" in text and "gueeve" not in text and "ghobz.com" not in text
