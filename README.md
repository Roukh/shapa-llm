# shapa

[![tests](https://github.com/Roukh/shapa-llm/actions/workflows/test.yml/badge.svg)](https://github.com/Roukh/shapa-llm/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/shapa.svg)](https://pypi.org/project/shapa/)
[![Python versions](https://img.shields.io/pypi/pyversions/shapa.svg)](https://pypi.org/project/shapa/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Roukh/shapa-llm/blob/main/LICENSE)

shapa gives coding agents memory and a work ledger. Each project keeps them in one SQLite file
that is checked into git. Claude Code loads them through hooks; Codex, OpenCode and any other MCP
client reach the same data through `shapa mcp`. An optional hosted store gives every agent
(coding, chat, phone) the same rules, tasks and memory.

## Install

```bash
uv tool install 'shapa[semantic,mcp]'    # or: pipx install shapa
```

The core needs Python 3.11 and no packages. `[semantic]` adds fused vector and BM25 recall,
`[mcp]` the MCP SDK. Upgrade with `uv tool upgrade shapa` (or `pipx upgrade shapa`), then run
`shapa upgrade --all --check`.

The package installs the command only. To wire Claude Code's hooks too, run the installer; it
also installs the command, from the repo's `main`, and a re-run upgrades it:

```bash
curl -fsSL https://raw.githubusercontent.com/Roukh/shapa-llm/main/bootstrap.sh | sh
```

Flags: `--with-semantic`, `--with-mcp`, `--full`, `--harness claude|codex|opencode|all`. The
header of [bootstrap.sh](https://github.com/Roukh/shapa-llm/blob/main/bootstrap.sh) lists the
rest. Hooks exist for Claude Code only; Codex and OpenCode are registered as MCP servers.

## Start

```bash
cd your-project && shapa init
shapa doctor
```

`init` creates `.shapa/` at the git top level (the wiki schema and `shapa.db`) and links two git
hooks: `pre-commit` keeps the database off feature branches, `post-commit` closes the job a
`J<n>:` commit names. It never writes into a shared `core.hooksPath` or a hook manager's
directory (husky, lefthook, pre-commit); it prints the lines to add there. `doctor` exits 1 and
names the fix when something is missing. Then use your agent as usual.

## Model

- **Wikis.** One per repo (`.shapa/`) and one global (`~/.shapa/memory`). A session reads both;
  a write goes to one (`--scope global|repo`).
- **Ledger.** Features (a capability: one branch and PR) hold jobs (one commit, subject
  `J<n>:`) and jobs hold tasks. Closing a merged feature sweeps closed, expired, duplicate and
  stale rows; git history of the database is the record.
- **Rows and documents.** Rows are memories, rules and issues. Documents are titled bodies kept
  whole, never swept. Notes are `arch/` and `research/` files. Recall ranks them by fused
  vectors and BM25 (`fused`), or BM25 alone without `[semantic]`.
- **Northstar and fires.** A session starts from one northstar and three ranked big fires
  (`shapa focus`) and only their tree. Closing a fire's feature promotes the next candidate.
- **Stages.** Stage 0 is the baseline every session starts with: rules, memories and skills in
  the global wiki, at most 6,000 characters, set only from the CLI (`shapa stage set ID 0`).
  The rest is fetched on demand by tag (`fetch --tag`), search or `related`.
- **Tasks and projects.** The global wiki holds your tasks, ideas and decisions and one file per
  project (`shapa project`); a project keeps one document.
- **Skills.** Versioned records, global or per repo. A rule, memory or issue that names a skill
  files a pending new version with the lesson quoted. Nothing goes live except through
  `shapa skill activate`. `shapa skill uproot NAME` moves a repo skill to the global wiki.
- **Learning loop.** A correction ("no", "not like this") becomes an issue row. A close match
  to an earlier one is a repeat. `shapa learn apply` files one principle, which you merge into
  the closest rule or skill (or add as new). It is global by default, so it holds across repos.
  `shapa metrics` shows whether it works.
- **Code partitions.** `shapa map` builds a derived graph of the checkout; `shapa partition`
  lists the files a fire touches, each with why. Local only.

## Commands

`shapa COMMAND --help` for flags. `shapa --version` prints the version.

| Command | Does |
|---|---|
| **Set up and check** | |
| `init` | create and register a wiki; `--global`, `--obsidian`, `--no-git`, `--upgrade-docs` |
| `doctor` | check the install; exit 1 when it needs a hand |
| `status` | the same report, never exit 1 |
| `where` | print the memory directory |
| `upgrade` | bring wikis to the current format; `--all`, `--check` |
| **Read** | |
| `bootstrap` | session start: northstar, fires and their tree, baseline; `--full` for all |
| `fetch` | rank memory for a prompt; `--tag T` reads a tag |
| `get ID` | the full text behind an id |
| `tags`, `related ID` | tags with counts; records linked to or sharing tags with one |
| `tasks` | open tasks across the wikis in scope |
| **Write** | |
| `save` | write one note or row; `--scope global\|repo\|external`, `--type` |
| `row` | memories, rules, issues: `add`, `edit`, `rm`, `link`, `tag`, `list` |
| `doc` | documents: `add`, `edit`, `rm`, `list`, `merge` |
| `project` | a project's one file: `new`, `add`, `show`, `list` |
| `capture` | Stop hook: extract memories from a finished session |
| `correction` | prompt hook: file an operator correction as an issue row |
| **Work** | |
| `ledger` | features, jobs, tasks: `add`, `claim`, `close`, `tree`, `issues`, git triggers |
| `focus` | northstar and three fires: `show`, `northstar`, `fire`, `order`, `promote` |
| `commit` | commit the wiki database on the default branch (a hook) |
| `map`, `partition` | code map of the checkout; a fire's bounded file set |
| **Learn** | |
| `stage` | `set`, `clear`, `list`, `pending` (records proposed for stage 0) |
| `skill` | `list`, `show`, `add`, `update`, `activate`, `uproot` |
| `learn` | `pending`, `apply`, `dismiss`, `merge`, `unchurn`, `principles`, `reindex` |
| `metrics` | whether the wiki is working; `--snapshot`, `--all`, `--history DAYS` |
| **Maintain** | |
| `maintain` | merge duplicates, prune stale rows; use `--dry-run`, prune is operator-run |
| `heartbeat`, `score` | prune orphan notes (`--dry-run`); rank notes by value |
| `validate` | check note frontmatter |
| **Serve** | |
| `mcp` | MCP stdio server (`search`, `get`, `save` and more) |
| `serve` | optional warm per-wiki daemon, latency only |
| `remote` | the hosted store: `login`, `sync`, `status`, `devices`, `logout`, `instructions` |

## Hosted

Off by default. Without a remote, shapa makes no network call for it. The store is one
Cloudflare Worker with one Durable Object that you deploy to your own account
([`hosted/`](https://github.com/Roukh/shapa-llm/tree/main/hosted), Node 22 or newer):

```bash
cd hosted && npm ci
npx wrangler secret put SHAPA_OWNER_KEY    # 32 or more characters
npx wrangler deploy
shapa remote login https://YOUR-WORKER-URL   # asks for the owner key
shapa remote sync
```

`login` registers this machine as a device (token in `~/.shapa`, mode 0600). `sync` exchanges
ledger items, rows, documents and `arch/` and `research/` notes both ways; `sync --hook` runs
inside a session hook within 8 seconds and never fails it. A repo with no git origin needs
`sync --key KEY` once. The URL must be HTTPS (HTTP only for localhost). The contract is
[hosted.md](https://github.com/Roukh/shapa-llm/blob/main/.shapa/arch/hosted.md).

**One brain for every agent.** `GET /` is a public card with no stored data: it names `/mcp`
(OAuth), `/v1` (device token) and `/openapi.json`. An agent calls `session_start` (or
`GET /v1/instructions`) and gets the same text as every other: northstar, fires, stage-0
baseline, topics, projects, your open tasks.

- **Reach.** An OAuth client (a chat app, a phone) reads the stage-0 baseline, notes, documents,
  tasks, ideas and decisions and nothing else. It cannot write rules, memories or skills, sync,
  set the focus or delete.
- **Stage 0** is written only from your own machine.
- Deploy the Worker before upgrading the CLI.

## Files and privacy

| Path | Holds |
|---|---|
| `<repo>/.shapa/shapa.db` | the repo wiki, tracked in git |
| `<repo>/.shapa/.shapa-index.db`, `.shapa-code.db` | derived search index and code map, git-ignored |
| `~/.shapa/memory/` | the global wiki, unless `init --global DIR` moved it |
| `~/.shapa/config.json`, `wikis.json` | global wiki pointer; registry of wikis |
| `~/.shapa/remote.json`, `remote.token`, `sync/` | only after `shapa remote login` |

`shapa commit` commits the database at session end on the default branch only, never pushes, and
redacts `scrub_terms` from `~/.shapa/config.json` first. Nothing leaves the machine unless a
remote is configured. Opt-in exceptions: `shapa remote`; the `[semantic]` extra fetches its
model once; `SHAPA_CAPTURE_DISTILL=1` and `shapa maintain --resolve` run the `claude` CLI.

Environment: `SHAPA_MEMORY` (wiki directory), `SHAPA_REMOTE_DIR` (login and sync state),
`SHAPA_OWNER_KEY` (read by `remote login`), `SHAPA_CAPTURE_DISTILL`, `SHAPA_CAPTURE_SUBAGENTS`,
`SHAPA_LEARNING=0` (no learning lines or metrics at session start).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[semantic,mcp]' pytest
.venv/bin/python -m pytest -q
```

See [CONTRIBUTING.md](https://github.com/Roukh/shapa-llm/blob/main/CONTRIBUTING.md).

## License

MIT, see [LICENSE](https://github.com/Roukh/shapa-llm/blob/main/LICENSE).
