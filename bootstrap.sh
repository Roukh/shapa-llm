#!/usr/bin/env sh
#
# shapa bootstrap - one-line install for the operational-memory tool.
#
#   curl -fsSL https://raw.githubusercontent.com/Roukh/shapa-llm/main/bootstrap.sh | sh
#
# It (1) installs the `shapa` command from the public repo (pipx > uv > pip
# --user, in that order) and (2) wires the Claude Code hooks + a default global
# wiki by running the repo's install.sh. No git clone required; the memory
# stays external to the tool. Re-running is idempotent.
#
# Extras (semantic search, MCP server): interactive by default (operator
# decision, shapa-backend-spec.md §10.3) - asks whether to install `[semantic]`
# and `[mcp]` when stdin is a TTY. With no TTY (the classic `curl | sh` pipe),
# it installs the core only and prints the exact follow-up command instead of
# guessing. Skip the prompt outright with a flag or env var:
#
#   curl -fsSL .../bootstrap.sh | sh -s -- --with-semantic   # or --with-mcp / --full
#
# MCP *registration* (wiring the shapa MCP server into a harness's own
# config, as opposed to installing the `[mcp]` SDK extra above) is a
# separate decision, forwarded to install.sh: --mcp/--no-mcp pin it,
# --harness claude|codex|opencode|all (default claude) picks the harness(es).
# Saying yes to "install MCP server support" at the interactive prompt also
# wires it by default (pass --no-mcp after to install the extra without
# wiring it). install.sh's own rule applies when neither is passed: prompt on
# a TTY, skip + print the exact command otherwise.
#
#   curl -fsSL .../bootstrap.sh | sh -s -- --with-mcp --harness codex
#
# Env overrides:
#   SHAPA_REF=main            git ref to install from
#   SHAPA_MEMORY=~/.shapa/memory   default (global) wiki location
#   SHAPA_WITH_SEMANTIC=1     same as --with-semantic (skips the prompt)
#   SHAPA_WITH_MCP=1          same as --with-mcp (skips the prompt)
#   SHAPA_FULL=1              same as --full (skips the prompt)
#   SHAPA_EMBEDDINGS=1        deprecated alias for SHAPA_WITH_SEMANTIC
#   SHAPA_NO_HOOKS=1          install the tool only, skip hook wiring

set -eu

REPO="https://github.com/Roukh/shapa-llm"
RAW="https://raw.githubusercontent.com/Roukh/shapa-llm"
REF="${SHAPA_REF:-main}"

say()  { printf '%s\n' "shapa: $*"; }
die()  { printf '%s\n' "shapa: ERROR: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have python3 || die "python3 (>=3.11) is required."
have curl    || die "curl is required."

# --- 0. extras: interactive prompt, or a flag/env var that skips it --------
# Deliberately decided BEFORE anything is installed, so one `pip`/`pipx`
# call carries whichever extras were chosen straight into the package spec
# below - no separate "now upgrade in place" step.
WITH_SEMANTIC=0
WITH_MCP=0
PROMPTED_OR_SKIPPED=0
MCP_WIRE=""     # "" = defer to install.sh's own auto logic; "--mcp"/"--no-mcp" pins it
HARNESS_ARG=""  # forwarded as install.sh's --harness (its own default: claude)

while [ $# -gt 0 ]; do
  case "$1" in
    --with-semantic) WITH_SEMANTIC=1; PROMPTED_OR_SKIPPED=1 ;;
    --with-mcp)      WITH_MCP=1; PROMPTED_OR_SKIPPED=1 ;;
    --full)          WITH_SEMANTIC=1; WITH_MCP=1; PROMPTED_OR_SKIPPED=1 ;;
    --mcp)           MCP_WIRE="--mcp" ;;
    --no-mcp)        MCP_WIRE="--no-mcp" ;;
    --harness)       HARNESS_ARG="$2"; shift ;;
    *) die "unknown argument: $1 (expected --with-semantic / --with-mcp / --full / --mcp / --no-mcp / --harness claude|codex|opencode|all)" ;;
  esac
  shift
done

if [ "${SHAPA_FULL:-0}" = "1" ]; then WITH_SEMANTIC=1; WITH_MCP=1; PROMPTED_OR_SKIPPED=1; fi
if [ "${SHAPA_WITH_SEMANTIC:-0}" = "1" ] || [ "${SHAPA_EMBEDDINGS:-0}" = "1" ]; then
  WITH_SEMANTIC=1; PROMPTED_OR_SKIPPED=1
fi
if [ "${SHAPA_WITH_MCP:-0}" = "1" ]; then WITH_MCP=1; PROMPTED_OR_SKIPPED=1; fi

# A flag/env var above already decided things - never prompt on top of that.
if [ "$PROMPTED_OR_SKIPPED" -eq 0 ]; then
  if [ -t 0 ] && [ -r /dev/tty ]; then
    printf 'shapa: install semantic search (local embeddings, extra download)? [y/N] ' >&2
    read -r reply < /dev/tty || reply=""
    case "$reply" in y|Y|yes|YES) WITH_SEMANTIC=1 ;; esac
    printf 'shapa: install MCP server support (for Codex/OpenCode/etc.)? [y/N] ' >&2
    read -r reply < /dev/tty || reply=""
    case "$reply" in y|Y|yes|YES) WITH_MCP=1 ;; esac
  else
    # No TTY (piped, e.g. `curl | sh`): never block on a read that would
    # just get EOF. Core-only is the honest default; tell the operator
    # exactly how to add the rest themselves.
    say "no TTY detected - installing the core (BM25-only) tool."
    say "add semantic search / MCP later with:"
    say "  pipx install --force 'shapa[semantic] @ git+${REPO}.git@${REF}'"
    say "  pipx install --force 'shapa[mcp] @ git+${REPO}.git@${REF}'"
    say "or re-run this script with --with-semantic / --with-mcp / --full."
  fi
fi

# Saying yes to the MCP extra (flag, env var, or the prompt above) also
# wires it into install.sh by default - an explicit --mcp/--no-mcp always
# wins over this inference, checked first in the arg loop.
if [ "$WITH_MCP" -eq 1 ] && [ -z "$MCP_WIRE" ]; then MCP_WIRE="--mcp"; fi

# Package spec: install from the git ref, folding in whatever extras were
# chosen above (comma-joined - pip/pipx read `pkg[a,b] @ url` as one extras
# list, not two separate installs).
EXTRAS=""
if [ "$WITH_SEMANTIC" -eq 1 ] && [ "$WITH_MCP" -eq 1 ]; then
  EXTRAS="semantic,mcp"
elif [ "$WITH_SEMANTIC" -eq 1 ]; then
  EXTRAS="semantic"
elif [ "$WITH_MCP" -eq 1 ]; then
  EXTRAS="mcp"
fi

SPEC="git+${REPO}.git@${REF}"
[ -n "$EXTRAS" ] && SPEC="shapa[${EXTRAS}] @ ${SPEC}"

# --- 1. install the tool ----------------------------------------------------
if have shapa; then
  say "already installed ($(command -v shapa)); reinstalling to ${REF}."
fi
if have pipx; then
  say "installing via pipx..."
  pipx install --force "$SPEC"
elif have uv; then
  say "installing via uv..."
  uv tool install --force "$SPEC"
else
  say "pipx/uv not found; installing via pip --user (consider pipx for isolation)."
  python3 -m pip install --user --upgrade "$SPEC"
fi

have shapa || die "install finished but 'shapa' is not on PATH. Add your user
  bin dir to PATH (pipx: 'pipx ensurepath') and re-run, or use SHAPA_NO_HOOKS=1."

# --- 2. wire the Claude Code hooks + default wiki ---------------------------
if [ "${SHAPA_NO_HOOKS:-0}" = "1" ]; then
  say "tool installed; skipping hook wiring (SHAPA_NO_HOOKS=1)."
  say "Wire later with: curl -fsSL ${RAW}/${REF}/install.sh | bash"
  exit 0
fi

if ! have jq; then
  say "jq not found - it is required to wire hooks."
  say "Install jq, then run: curl -fsSL ${RAW}/${REF}/install.sh | bash"
  say "The 'shapa' tool itself is installed and usable now."
  exit 0
fi

say "wiring Claude Code hooks (install.sh)..."
INSTALL_SH="$(mktemp)"
trap 'rm -f "$INSTALL_SH"' EXIT
curl -fsSL "${RAW}/${REF}/install.sh" -o "$INSTALL_SH"
# Pass --memory/--mcp/--no-mcp/--harness only when set, preserving a path
# that may contain spaces (set -- is the POSIX-sh-safe way to build an argv
# list without arrays).
set --
[ -n "${SHAPA_MEMORY:-}" ] && set -- --memory "$SHAPA_MEMORY"
[ -n "$MCP_WIRE" ] && set -- "$@" "$MCP_WIRE"
[ -n "$HARNESS_ARG" ] && set -- "$@" --harness "$HARNESS_ARG"
# install.sh finds the pipx-installed `shapa` on PATH; no clone needed. This
# unconditionally connects/inits the GLOBAL default wiki (§3) even when cwd
# has its own repo-local wiki, closing the "resolves to a wiki that doesn't
# exist on disk" gap the empirical probe found.
bash "$INSTALL_SH" "$@"

cat <<EOF

shapa is installed and wired.
  - Global memory (LLM rules) lives at the default wiki; browse it in Obsidian.
  - In any project:  cd <repo> && shapa init   (creates ./shapa, auto-resolved
    for every session run inside that repo).
EOF
