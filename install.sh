#!/usr/bin/env bash
#
# shapa install.sh
#
# Wires shapa into the local Claude Code config and sets up the memory vault.
# Works whether shapa is installed (pipx: `shapa` on PATH) or run from a clone
# (a repo .venv is created with embeddings). Memory is EXTERNAL to the tool:
# $SHAPA_MEMORY or ~/.shapa/memory - private notes never live in the repo.
#
# Hooks installed (idempotent, --harness claude|all only):
#   SessionStart     -> shapa bootstrap         (read: metadata-only overview, once/session)
#   UserPromptSubmit -> shapa fetch             (read: relevant memory)
#   Stop             -> shapa capture           (write: distil the session)
#   Stop             -> shapa maintain --prune   (prune orphans/stale + merge dupes)
#   SubagentStop     -> shapa capture
#
# MCP server registration (--mcp, gated per --harness; skipped for any
# harness whose binary isn't on PATH - see the mcp_* functions below for the
# exact config surface + doc citation each one targets):
#   claude   -> `claude mcp add`/`remove` --scope user (user-scope, all projects)
#   codex    -> ~/.codex/config.toml [mcp_servers.shapa] (BEGIN/END-marked block)
#   opencode -> opencode.json's "mcp" key (jq merge)
#
# Skill: installs the shapa-upgrade skill for every harness in scope, then
# finishes with `shapa upgrade --all --check` - which wikis need that skill.
#
# Obsidian: scaffolds the memory dir as a vault and registers it if installed.
#
# Usage:
#   ./install.sh                 # install (memory at ~/.shapa/memory)
#   ./install.sh --memory DIR    # use a specific memory directory
#   ./install.sh --harness claude|codex|opencode|all   # default: claude
#   ./install.sh --mcp | --no-mcp                      # wire/skip MCP registration
#                                 # (unspecified: prompts on a TTY, else skips
#                                 # and prints the exact command; --dry-run
#                                 # always previews the plan as if --mcp)
#   ./install.sh --dry-run | --settings PATH | --no-obsidian | --no-embeddings | --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SETTINGS=""; DRY_RUN=0; UNINSTALL=0; NO_OBSIDIAN=0; NO_EMBEDDINGS=0; MEMORY=""
HARNESS="claude"; MCP=""   # MCP: "" = auto (TTY prompt / no-TTY skip), "1"/"0" = pinned
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)       DRY_RUN=1 ;;
    --uninstall)     UNINSTALL=1 ;;
    --no-obsidian)   NO_OBSIDIAN=1 ;;
    --no-embeddings) NO_EMBEDDINGS=1 ;;
    --memory)        MEMORY="$2"; shift ;;
    --settings)      SETTINGS="$2"; shift ;;
    --harness)       HARNESS="$2"; shift ;;
    --mcp)           MCP=1 ;;
    --no-mcp)        MCP=0 ;;
    -h|--help)       sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$HARNESS" in
  claude|codex|opencode|all) ;;
  *) echo "unknown --harness value: '$HARNESS' (expected claude|codex|opencode|all)" >&2; exit 2 ;;
esac
# True when *harness* ($1) is the one selected, or "all" was: the single
# predicate every hook/MCP block below gates on, so --harness routes
# registration to the right surface without duplicating this check.
harness_in_scope() { [ "$HARNESS" = "$1" ] || [ "$HARNESS" = "all" ]; }

[ -z "$SETTINGS" ] && SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[ -z "$MEMORY" ] && MEMORY="${SHAPA_MEMORY:-$HOME/.shapa/memory}"

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required." >&2; exit 1; }

# --- resolve the shapa invocation (pipx 'shapa', or a repo .venv) -----------
resolve_shapa() {
  if command -v shapa >/dev/null 2>&1; then echo "shapa"; return; fi
  local vbin="$REPO_DIR/.venv/bin/shapa"
  if [ -x "$vbin" ]; then echo "$vbin"; return; fi
  if [ "$NO_EMBEDDINGS" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "Setting up repo .venv with shapa + semantic extras (one-time)..." >&2
    python3 -m venv "$REPO_DIR/.venv" >&2 2>&1 || true
    "$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR"'[semantic]' >&2 2>&1 || \
      "$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR" >&2 2>&1 || true
    [ -x "$vbin" ] && { echo "$vbin"; return; }
  fi
  echo "python3 -m shapa"   # last resort (BM25 fallback; run from the repo)
}
INV="$(resolve_shapa)"

# Hooks resolve the wiki via the pointer that `shapa init --global` records below (see
# shapa/config.py). We deliberately do NOT bake SHAPA_MEMORY into the hook
# command: that way a later `shapa init --global /new/path` moves the wiki and the hooks
# follow it automatically, instead of silently reading the old baked-in path.
#
# SHAPA_SERVE_AUTOSTART=1 (GAP D, shapa-backend-spec.md §5): the SessionStart
# hook autostarts each in-scope wiki's `shapa serve` daemon detached in the
# background (idle-timeout, never blocks session start on any failure - see
# bootstrap.py's `_maybe_autostart_daemons`), so `shapa fetch`'s per-prompt
# embedding lookups skip reloading the model2vec model on every single
# invocation once the daemon is up. Bare `shapa serve`/`bootstrap.main()`
# calls (tests, manual runs) stay opt-out by default - only this installed
# hook command turns it on.
BOOTSTRAP_CMD="SHAPA_SERVE_AUTOSTART=1 $INV bootstrap"
FETCH_CMD="$INV fetch"
CAPTURE_CMD="$INV capture"
MAINTAIN_CMD="$INV maintain --prune"

EVENTS=("SessionStart"    "UserPromptSubmit" "Stop"         "Stop"          "SubagentStop")
CMDS=(  "$BOOTSTRAP_CMD"  "$FETCH_CMD"        "$CAPTURE_CMD" "$MAINTAIN_CMD" "$CAPTURE_CMD")

# The argv a harness should run to speak to the shapa MCP server: the same
# invocation as the hooks above plus a trailing "mcp" subcommand. $INV is
# always either a single path/name ("shapa", or an absolute .venv path) or
# the two-word fallback "python3 -m shapa" (see resolve_shapa() above) - both
# are safe to word-split on whitespace, which is exactly what an unquoted
# here-string read does; this is never attacker-controlled input.
read -ra MCP_ARGV <<< "$INV mcp"
MCP_SERVER_NAME="shapa"

have_bin() { command -v "$1" >/dev/null 2>&1; }

# --- Claude Code: `claude mcp add`/`remove --scope user` --------------------
# Source: https://code.claude.com/docs/en/mcp-quickstart ("Add a local
# server" + "Change server scope"). A local stdio server is registered with
# `claude mcp add <name> -- <command> [args...]`; `--scope user` makes it
# active in every project (else it defaults to `local`, tied to the project
# it was added from). `claude mcp get <name>` reports which scope (if any)
# already holds the name, across all scopes - the idempotency check below.
mcp_claude() {  # $1 = add|remove
  have_bin claude || { echo "claude CLI not found on PATH - skipping Claude Code MCP registration."; return 0; }
  if [ "$1" = "remove" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then echo "# would run: claude mcp remove $MCP_SERVER_NAME --scope user"; return 0; fi
    claude mcp remove "$MCP_SERVER_NAME" --scope user >/dev/null 2>&1 || true
    echo "Removed Claude Code MCP registration for '$MCP_SERVER_NAME' (if present)."
    return 0
  fi
  if claude mcp get "$MCP_SERVER_NAME" >/dev/null 2>&1; then
    echo "Claude Code MCP server '$MCP_SERVER_NAME' already registered - skipping (idempotent)."
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "# would run: claude mcp add $MCP_SERVER_NAME --scope user -- ${MCP_ARGV[*]}"
    return 0
  fi
  claude mcp add "$MCP_SERVER_NAME" --scope user -- "${MCP_ARGV[@]}"
  echo "Registered Claude Code MCP server '$MCP_SERVER_NAME' (user scope)."
}

# --- Codex: ~/.codex/config.toml [mcp_servers.<name>] -----------------------
# Source: https://developers.openai.com/codex/config-reference and
# https://github.com/openai/codex/blob/main/docs/config.md - a stdio MCP
# server is declared as a `[mcp_servers.<id>]` table with `command` (string)
# and `args` (string array) keys; there is no documented `codex mcp add` CLI,
# so this edits the TOML file directly. The block is wrapped in BEGIN/END
# marker comments so re-running is idempotent (skip if the markers are
# already present) and --uninstall can delete exactly this block - never any
# other hand-written `[mcp_servers.*]` entry - with a plain line-range `sed`.
CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
CODEX_BEGIN="# BEGIN shapa-mcp"
CODEX_END="# END shapa-mcp"

_toml_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

mcp_codex() {  # $1 = add|remove
  have_bin codex || { echo "codex CLI not found on PATH - skipping Codex MCP registration."; return 0; }
  if [ "$1" = "remove" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then echo "# would remove the shapa-mcp block from $CODEX_CONFIG"; return 0; fi
    if [ -f "$CODEX_CONFIG" ]; then
      sed -i "/^${CODEX_BEGIN}\$/,/^${CODEX_END}\$/d" "$CODEX_CONFIG"
    fi
    echo "Removed shapa MCP block from $CODEX_CONFIG (if present)."
    return 0
  fi
  if [ -f "$CODEX_CONFIG" ] && grep -qF "$CODEX_BEGIN" "$CODEX_CONFIG"; then
    echo "Codex MCP server '$MCP_SERVER_NAME' already registered at $CODEX_CONFIG - skipping (idempotent)."
    return 0
  fi
  local cmd_esc args_toml arg esc BLOCK
  cmd_esc="$(_toml_escape "${MCP_ARGV[0]}")"
  args_toml=""
  for arg in "${MCP_ARGV[@]:1}"; do
    esc="$(_toml_escape "$arg")"
    args_toml="${args_toml}\"${esc}\", "
  done
  args_toml="[${args_toml%, }]"
  BLOCK="$CODEX_BEGIN
# managed by \`shapa install.sh\` - edit via --mcp/--uninstall, not by hand
[mcp_servers.$MCP_SERVER_NAME]
command = \"$cmd_esc\"
args = $args_toml
$CODEX_END"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "# would append to $CODEX_CONFIG:"
    printf '%s\n' "$BLOCK"
    return 0
  fi
  mkdir -p "$(dirname "$CODEX_CONFIG")"
  touch "$CODEX_CONFIG"
  if [ -s "$CODEX_CONFIG" ]; then printf '\n' >> "$CODEX_CONFIG"; fi
  printf '%s\n' "$BLOCK" >> "$CODEX_CONFIG"
  echo "Registered Codex MCP server '$MCP_SERVER_NAME' in $CODEX_CONFIG."
}

# --- OpenCode: opencode.json's "mcp" key ------------------------------------
# Source: https://opencode.ai/docs/mcp-servers/ - each server is a key
# directly under the top-level "mcp" object (NOT "mcp.servers"); a local
# stdio server is `{"type": "local", "command": [...]}`. Config path per the
# same docs' Config page: global config at
# "$XDG_CONFIG_HOME/opencode/opencode.json" (default ~/.config/opencode/...).
OPENCODE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"

mcp_opencode() {  # $1 = add|remove
  have_bin opencode || { echo "opencode CLI not found on PATH - skipping OpenCode MCP registration."; return 0; }
  local cmd_json OC_MERGED OC_TMP
  if [ "$1" = "remove" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then echo "# would remove .mcp.$MCP_SERVER_NAME from $OPENCODE_CONFIG"; return 0; fi
    if [ -f "$OPENCODE_CONFIG" ]; then
      OC_MERGED="$(jq --arg n "$MCP_SERVER_NAME" 'if .mcp then .mcp |= del(.[$n]) else . end' "$OPENCODE_CONFIG")"
      OC_TMP="$(mktemp)"; printf '%s\n' "$OC_MERGED" > "$OC_TMP" && mv "$OC_TMP" "$OPENCODE_CONFIG"
    fi
    echo "Removed OpenCode MCP registration for '$MCP_SERVER_NAME' (if present)."
    return 0
  fi
  if [ -f "$OPENCODE_CONFIG" ] && jq -e --arg n "$MCP_SERVER_NAME" '(.mcp // {}) | .[$n] != null' "$OPENCODE_CONFIG" >/dev/null 2>&1; then
    echo "OpenCode MCP server '$MCP_SERVER_NAME' already registered at $OPENCODE_CONFIG - skipping (idempotent)."
    return 0
  fi
  cmd_json="$(printf '%s\n' "${MCP_ARGV[@]}" | jq -R . | jq -s .)"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "# would set .mcp.$MCP_SERVER_NAME in $OPENCODE_CONFIG to:"
    jq -n --argjson cmd "$cmd_json" '{type:"local", command:$cmd}'
    return 0
  fi
  mkdir -p "$(dirname "$OPENCODE_CONFIG")"
  [ -f "$OPENCODE_CONFIG" ] || echo '{}' > "$OPENCODE_CONFIG"
  OC_MERGED="$(jq --arg n "$MCP_SERVER_NAME" --argjson cmd "$cmd_json" \
    '.mcp = (.mcp // {}) | .mcp[$n] = {type:"local", command:$cmd}' "$OPENCODE_CONFIG")"
  OC_TMP="$(mktemp)"; printf '%s\n' "$OC_MERGED" > "$OC_TMP" && mv "$OC_TMP" "$OPENCODE_CONFIG"
  echo "Registered OpenCode MCP server '$MCP_SERVER_NAME' in $OPENCODE_CONFIG."
}

# --- the shapa-upgrade skill (shapa-backend-spec.md §11) -------------------
# The skill text ships inside the installed package (shapa/assets/skills/),
# so a curl install with no clone gets the same file: `shapa upgrade
# --print-skill` prints it. Skill homes, per each harness's own docs:
#   claude   -> <claude config dir>/skills/<name>/SKILL.md (next to $SETTINGS)
#   codex    -> ~/.agents/skills/<name>/SKILL.md
#               (https://learn.chatgpt.com/docs/build-skills, user scope)
#   opencode -> $XDG_CONFIG_HOME/opencode/skills/<name>/SKILL.md
#               (https://opencode.ai/docs/skills/)
# codex/opencode are skipped when their binary isn't on PATH, like MCP.
SKILL_NAME="shapa-upgrade"
read -ra INV_ARGV <<< "$INV"

skill_dir_for() {  # $1 = harness
  case "$1" in
    claude)   echo "$(dirname "$SETTINGS")/skills/$SKILL_NAME" ;;
    codex)    echo "$HOME/.agents/skills/$SKILL_NAME" ;;
    opencode) echo "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/$SKILL_NAME" ;;
  esac
}

skill_one() {  # $1 = add|remove, $2 = harness
  local dir text
  [ "$2" = "claude" ] || have_bin "$2" || return 0
  dir="$(skill_dir_for "$2")"
  if [ "$1" = "remove" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then echo "# would remove $dir/SKILL.md"; return 0; fi
    rm -f "$dir/SKILL.md"; rmdir "$dir" 2>/dev/null || true
    echo "Removed the $SKILL_NAME skill for $2 (if present)."
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then echo "# would install the $SKILL_NAME skill to $dir/SKILL.md"; return 0; fi
  text="$("${INV_ARGV[@]}" upgrade --print-skill 2>/dev/null)" || text=""
  if [ -z "$text" ]; then
    echo "shapa: this shapa has no bundled $SKILL_NAME skill - skipping $2." >&2
    return 0
  fi
  mkdir -p "$dir"
  printf '%s\n' "$text" > "$dir/SKILL.md"
  echo "Installed the $SKILL_NAME skill for $2: $dir/SKILL.md"
}

run_skill() {  # $1 = add|remove - every harness in scope
  local h
  for h in claude codex opencode; do
    harness_in_scope "$h" && skill_one "$1" "$h"
  done
  return 0
}

# Last step of every install/update: which known wikis are behind this
# shapa's format. Informational - never fails the install.
upgrade_check() {
  if [ "$DRY_RUN" -eq 1 ]; then echo "# would run: $INV upgrade --all --check"; return 0; fi
  echo "Checking every known wiki against this shapa's format:"
  if "${INV_ARGV[@]}" upgrade --all --check; then
    echo "Every known wiki is current."
  else
    echo "The wikis marked 'behind' above need the $SKILL_NAME skill: ask your agent to run it."
  fi
  return 0
}

run_mcp() {  # $1 = add|remove - dispatches to every harness in scope
  harness_in_scope claude   && mcp_claude   "$1"
  harness_in_scope codex    && mcp_codex    "$1"
  harness_in_scope opencode && mcp_opencode "$1"
  return 0
}

# --- resolve MCP=auto: a TTY gets asked, a pipe gets skipped + told how -----
# (decision 3's bootstrap.sh prompt pattern, applied here to registration
# rather than pip extras: never guess, never block on a read that would just
# get EOF.) --uninstall defaults an unset flag to "yes, clean up" since
# removal is idempotent and safe to attempt even where nothing was wired.
# --dry-run defaults an unset flag to "yes, preview" so the plan is visible;
# pass --no-mcp explicitly to preview with MCP left out.
if [ -z "$MCP" ]; then
  if [ "$UNINSTALL" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
    MCP=1
  elif [ -t 0 ] && [ -r /dev/tty ]; then
    printf 'shapa: register the shapa MCP server for %s now (user scope)? [y/N] ' "$HARNESS" >&2
    read -r reply < /dev/tty || reply=""
    case "$reply" in y|Y|yes|YES) MCP=1 ;; *) MCP=0 ;; esac
  else
    MCP=0
    echo "shapa: no TTY - not wiring MCP registration. Add it later with:" >&2
    echo "  $0 --mcp --harness $HARNESS" >&2
  fi
fi

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

# Bootstrap the settings file's directory/contents before either path below
# touches it - install writes hooks into it, uninstall reads-then-rewrites it
# via write_settings()'s mktemp+mv, which fails if dirname($SETTINGS) doesn't
# exist yet (e.g. a fresh machine, or one only ever wired for codex/opencode).
# Runs for BOTH --uninstall and install, same as before harnesses existed.
if harness_in_scope claude; then
  mkdir -p "$(dirname "$SETTINGS")"; [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
fi

if [ "$UNINSTALL" -eq 1 ]; then
  if harness_in_scope claude; then
    MERGED="$(cat "$SETTINGS" 2>/dev/null || echo '{}')"
    for i in "${!EVENTS[@]}"; do
      MERGED="$(printf '%s' "$MERGED" | jq --arg ev "${EVENTS[$i]}" --arg cmd "${CMDS[$i]}" '
        if .hooks[$ev] then .hooks[$ev] |= map(.hooks |= map(select(.command != $cmd)))
          | .hooks[$ev] |= map(select((.hooks | length) > 0)) else . end')"
    done
    write_settings "$MERGED"
    echo "shapa hooks removed from $SETTINGS"
  fi
  [ "$MCP" -eq 1 ] && run_mcp remove
  run_skill remove
  exit 0
fi

# Connect the wiki before wiring anything at it: `shapa init` creates the dir,
# installs the design docs (arch/ + AGENTS.md), and records the path (--global). This
# runs regardless of --harness: Codex/OpenCode's MCP tools read/write this
# same wiki, so it must exist before any harness is wired to reach it.
[ "$DRY_RUN" -eq 0 ] && { mkdir -p "$MEMORY"; "${INV_ARGV[@]}" init --global "$MEMORY" >/dev/null 2>&1 || true; }

if harness_in_scope claude; then
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
fi

[ "$MCP" -eq 1 ] && run_mcp add
run_skill add

[ "$DRY_RUN" -eq 1 ] && { upgrade_check; exit 0; }
if harness_in_scope claude; then
  echo "Installed shapa hooks into $SETTINGS (memory: $MEMORY):"
  echo "  SessionStart     -> $BOOTSTRAP_CMD"
  echo "  UserPromptSubmit -> $INV fetch"
  echo "  Stop             -> $INV capture ; $INV maintain --prune"
  echo "  SubagentStop     -> $INV capture"
  echo "maintain --prune deletes orphan/stale notes and auto-merges duplicates."
  echo "Preview anytime:  SHAPA_MEMORY=$MEMORY $INV maintain --dry-run"
else
  echo "shapa memory wiki connected (memory: $MEMORY); --harness $HARNESS skips Claude Code hooks."
fi
upgrade_check
