#!/usr/bin/env bash
#
# shapa install.sh
#
# Wires shapa into the local Claude Code config and sets up the memory vault.
# Works whether shapa is installed (pipx: `shapa` on PATH) or run from a clone
# (a repo .venv is created with embeddings). Memory is EXTERNAL to the tool:
# $SHAPA_MEMORY or ~/.shapa/memory - private notes never live in the repo.
#
# Hooks installed (idempotent):
#   UserPromptSubmit -> shapa fetch             (read: relevant memory)
#   Stop             -> shapa capture           (write: distil the session)
#   Stop             -> shapa maintain --prune   (prune orphans/stale + merge dupes)
#   SubagentStop     -> shapa capture
#
# Obsidian: scaffolds the memory dir as a vault and registers it if installed.
#
# Usage:
#   ./install.sh                 # install (memory at ~/.shapa/memory)
#   ./install.sh --memory DIR    # use a specific memory directory
#   ./install.sh --dry-run | --settings PATH | --no-obsidian | --no-embeddings | --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SETTINGS=""; DRY_RUN=0; UNINSTALL=0; NO_OBSIDIAN=0; NO_EMBEDDINGS=0; MEMORY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)       DRY_RUN=1 ;;
    --uninstall)     UNINSTALL=1 ;;
    --no-obsidian)   NO_OBSIDIAN=1 ;;
    --no-embeddings) NO_EMBEDDINGS=1 ;;
    --memory)        MEMORY="$2"; shift ;;
    --settings)      SETTINGS="$2"; shift ;;
    -h|--help)       sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -z "$SETTINGS" ] && SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[ -z "$MEMORY" ] && MEMORY="${SHAPA_MEMORY:-$HOME/.shapa/memory}"

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required." >&2; exit 1; }

# --- resolve the shapa invocation (pipx 'shapa', or a repo .venv) -----------
resolve_shapa() {
  if command -v shapa >/dev/null 2>&1; then echo "shapa"; return; fi
  local vbin="$REPO_DIR/.venv/bin/shapa"
  if [ -x "$vbin" ]; then echo "$vbin"; return; fi
  if [ "$NO_EMBEDDINGS" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "Setting up repo .venv with shapa + embeddings (one-time)..." >&2
    python3 -m venv "$REPO_DIR/.venv" >&2 2>&1 || true
    "$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR"'[embeddings]' >&2 2>&1 || \
      "$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR" >&2 2>&1 || true
    [ -x "$vbin" ] && { echo "$vbin"; return; }
  fi
  echo "python3 -m shapa"   # last resort (BM25 fallback; run from the repo)
}
INV="$(resolve_shapa)"

# Hooks resolve the wiki via the pointer that `shapa init` records below (see
# shapa/config.py). We deliberately do NOT bake SHAPA_MEMORY into the hook
# command: that way a later `shapa init /new/path` moves the wiki and the hooks
# follow it automatically, instead of silently reading the old baked-in path.
FETCH_CMD="$INV fetch"
CAPTURE_CMD="$INV capture"
MAINTAIN_CMD="$INV maintain --prune"

EVENTS=("UserPromptSubmit" "Stop"         "Stop"          "SubagentStop")
CMDS=(  "$FETCH_CMD"        "$CAPTURE_CMD" "$MAINTAIN_CMD" "$CAPTURE_CMD")

mkdir -p "$(dirname "$SETTINGS")"; [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

write_settings() { if [ "$DRY_RUN" -eq 1 ]; then echo "$1"; else
  TMP="$(mktemp)"; echo "$1" > "$TMP" && mv "$TMP" "$SETTINGS"; fi; }

wire_obsidian() {
  [ "$NO_OBSIDIAN" -eq 1 ] && return 0
  if [ "$DRY_RUN" -eq 1 ]; then echo "# would set up Obsidian vault at $MEMORY"; return 0; fi
  mkdir -p "$MEMORY/.obsidian"
  [ -f "$MEMORY/.obsidian/app.json" ] || echo '{}' > "$MEMORY/.obsidian/app.json"
  [ -f "$MEMORY/.obsidian/core-plugins.json" ] || \
    echo '{"graph":true,"backlink":true,"outgoing-link":true}' > "$MEMORY/.obsidian/core-plugins.json"
  local obs=""
  for c in "$HOME/.config/obsidian/obsidian.json" \
           "$HOME/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json"; do
    [ -f "$c" ] && { obs="$c"; break; }
  done
  [ -z "$obs" ] && { echo "Obsidian config not found; open $MEMORY as a vault manually."; return 0; }
  local id ts merged
  id="$(printf '%s' "$MEMORY" | sha1sum | cut -c1-16)"
  ts="$(date +%s%3N 2>/dev/null || echo "$(date +%s)000")"
  merged="$(jq --arg id "$id" --arg path "$MEMORY" --argjson ts "$ts" '
    .vaults = (.vaults // {})
    | if ([.vaults[].path] | index($path)) == null then .vaults[$id] = {path:$path, ts:$ts, open:false} else . end
  ' "$obs")"
  TMP="$(mktemp)"; echo "$merged" > "$TMP" && mv "$TMP" "$obs"
  echo "Registered Obsidian vault: $MEMORY"
}

if [ "$UNINSTALL" -eq 1 ]; then
  MERGED="$(cat "$SETTINGS")"
  for i in "${!EVENTS[@]}"; do
    MERGED="$(printf '%s' "$MERGED" | jq --arg ev "${EVENTS[$i]}" --arg cmd "${CMDS[$i]}" '
      if .hooks[$ev] then .hooks[$ev] |= map(.hooks |= map(select(.command != $cmd)))
        | .hooks[$ev] |= map(select((.hooks | length) > 0)) else . end')"
  done
  write_settings "$MERGED"; echo "shapa hooks removed from $SETTINGS"; exit 0
fi

# Connect the wiki before wiring anything at it: `shapa init` creates the dir,
# installs the design docs (arch/ + AGENTS.md), and records the path.
[ "$DRY_RUN" -eq 0 ] && { mkdir -p "$MEMORY"; "$INV" init "$MEMORY" >/dev/null 2>&1 || true; }

MERGED="$(cat "$SETTINGS")"
for i in "${!EVENTS[@]}"; do
  ev="${EVENTS[$i]}"; cmd="${CMDS[$i]}"
  present="$(printf '%s' "$MERGED" | jq --arg ev "$ev" --arg cmd "$cmd" \
    '[.hooks[$ev][]?.hooks[]? | select(.command == $cmd)] | length')"
  [ "$present" != "0" ] && continue
  MERGED="$(printf '%s' "$MERGED" | jq --arg ev "$ev" --arg cmd "$cmd" '
    .hooks = (.hooks // {})
    | .hooks[$ev] = ((.hooks[$ev] // []) + [
        { "matcher": "", "hooks": [ { "type": "command", "command": $cmd, "timeout": 60 } ] } ])')"
done
write_settings "$MERGED"

wire_obsidian

[ "$DRY_RUN" -eq 1 ] && exit 0
echo "Installed shapa hooks into $SETTINGS (memory: $MEMORY):"
echo "  UserPromptSubmit -> $INV fetch"
echo "  Stop             -> $INV capture ; $INV maintain --prune"
echo "  SubagentStop     -> $INV capture"
echo "maintain --prune deletes orphan/stale notes and auto-merges duplicates."
echo "Preview anytime:  SHAPA_MEMORY=$MEMORY $INV maintain --dry-run"
