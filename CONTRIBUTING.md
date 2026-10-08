# Contributing

Thanks for looking at shapa. This project has one maintainer
([@Roukh](https://github.com/Roukh)), so please open an issue before a large
change; a small fix or a clear bug report can go straight to a pull request.

## Dev setup

```bash
git clone https://github.com/Roukh/shapa-llm
cd shapa-llm
python3 -m venv .venv
.venv/bin/pip install -e '.[semantic,mcp]'
.venv/bin/pip install pytest
```

Run the suite from the repo root:

```bash
.venv/bin/python -m pytest -q
```

Two tests need a real filesystem socket and network access that some
sandboxes block: `tests/test_serve.py` (Unix domain sockets) and
`tests/test_installer_upgrade.py`. If they fail only inside a sandboxed
shell, run them again outside it before treating the suite as red.

CI (`.github/workflows/test.yml`) runs the same suite on Python 3.11, 3.12
and 3.13, twice per version: once against the bare core install, and once
with every optional extra (`[semantic,mcp]`). A change that only works with
an extra installed, or only without one, fails half the matrix; test both
paths locally before you push.

## House rules

- **The core engine stays pure standard library.** `dependencies = []` in
  `pyproject.toml` is load-bearing. A new capability that needs a package
  goes behind an optional extra (`[semantic]`, `[mcp]`, or a new one), and
  the core path keeps working with that extra absent.
- **Tests run against temporary wikis.** `tests/conftest.py` points
  `$SHAPA_REGISTRY` at a throwaway file. Never point a test, a script, or a
  manual run at `~/.shapa` or a real project's `.shapa/`.
- **This repository is public.** Everything tracked, including `.shapa/`
  (this repo's own wiki), is published on push. Keep secrets, API keys,
  other people's names, other projects' details, and private wiki content
  out of code, tests, fixtures, docs and commit messages.
- **Hooks never block a session.** `capture` and `maintain`, run as hooks,
  always exit 0 and never write inside the repo or the installed package.
- **Destructive commands are opt-in and explicit.** `shapa maintain
  --prune` and a non-dry-run heartbeat mutate a real wiki; a pull request
  should not run either against anything but a test fixture.
- **One SQLite file per wiki, committed only on the default branch.** A
  change to the database schema or the git-hook contract around it needs a
  test, not only a manual check.

## Making a change

1. Fork or branch, make the change, and add or update tests under `tests/`.
2. Run the full suite (core and `[semantic,mcp]`) before opening the pull
   request.
3. Describe what changed and why in the pull request body; link the issue
   it closes, if there is one.
4. CI must pass on all three Python versions and both extras combinations.

## Releasing

Maintainer-only; see [RELEASING.md](RELEASING.md).
