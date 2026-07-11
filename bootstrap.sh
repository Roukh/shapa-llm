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
# Env overrides:
#   SHAPA_REF=main            git ref to install from
#   SHAPA_MEMORY=~/.shapa/memory   default (global) wiki location
#   SHAPA_EMBEDDINGS=1        also install local embeddings (sentence-transformers)
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

# Package spec: install from the git ref. Optionally pull in the embeddings extra.
SPEC="git+${REPO}.git@${REF}"
if [ "${SHAPA_EMBEDDINGS:-0}" = "1" ]; then
  SPEC="shapa[embeddings] @ ${SPEC}"
fi

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
# Pass --memory only when set, preserving a path that may contain spaces.
set --
[ -n "${SHAPA_MEMORY:-}" ] && set -- --memory "$SHAPA_MEMORY"
# install.sh finds the pipx-installed `shapa` on PATH; no clone needed.
bash "$INSTALL_SH" "$@"

cat <<EOF

shapa is installed and wired.
  - Global memory (LLM rules) lives at the default wiki; browse it in Obsidian.
  - In any project:  cd <repo> && shapa init   (creates ./shapa, auto-resolved
    for every session run inside that repo).
EOF
