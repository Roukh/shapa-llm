"""The MCP stdio server - the vendor-neutral, on-demand surface every MCP
client (Codex, OpenCode, anything else that speaks the protocol) can reach
today (shapa-backend-spec.md §7.1).

Exposes four tools:

  search(query, k=8, root=None)
      Ranked search across every wiki in scope (:func:`shapa.fetch.select_multi`)
      - the same merge Tier 2/3 use, snippet-truncated.
  get(id, root=None)
      One note's FULL body by id (Tier 3 - no snippet truncation), searched
      across every wiki in scope. Reports the id as ambiguous (rather than
      picking one) when it exists in more than one root - the same
      never-silently-decide contract ``fetch.py``'s F09 collision handling
      already has for reads.
  save(scope, summary, body, type="memory", tags=None, id=None, root=None)
      A scope-gated write. ``scope`` is REQUIRED and is exactly
      ``"global"`` or ``"repo"`` (operator decision, spec §10 #4/#6 -
      ``"external"`` is retired); there is no silent default, matching
      ``placement.md``'s "never guess" rule. ``repo`` requires a repo-local
      wiki to already exist (``shapa init`` first) - it is never created as
      a side effect of a write.
  placement()
      The scope decision rule itself (``shapa/assets/placement.md``,
      installed into every wiki by ``shapa init``), for a client that wants
      to ask before it writes.

Transport: a stdlib-only, dependency-free line-delimited JSON-RPC 2.0 loop
over stdio implementing the minimal MCP surface (``initialize``,
``tools/list``, ``tools/call``) - this framing is exactly what a real MCP
stdio client speaks, so it interoperates whether or not the optional
``mcp`` SDK package (the ``[mcp]`` extra) happens to be installed; this
module never imports it. This is the "stdlib-only JSON-RPC shim" §3/§7.1
describe, and here it is the whole implementation, not just a fallback
branch - see the Slice 6 report for why.

CLI::

    shapa mcp                 # run the stdio server (stdin/stdout, blocks)
    python3 -m shapa.mcp --cwd /path/to/repo   # pin wiki discovery (testing)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shapa import __version__, config, fetch, frontmatter, memlog
from shapa.nodes import load_nodes
from shapa.validate import validate_node

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
PLACEMENT_ASSET = ASSETS_DIR / "placement.md"

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "shapa"

VALID_SAVE_SCOPES = {"global", "repo"}
VALID_NOTE_TYPES = {"memory", "rule", "issue", "document", "idea"}
MAX_SUMMARY_CHARS = 160  # F04

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:60] if slug else f"note-{uuid.uuid4().hex[:8]}"


def _is_safe_note_id(note_id: str) -> bool:
    """Reject anything that could turn ``root / f"{note_id}.md"`` into a
    path outside ``root`` - a caller-supplied ``id`` is attacker/agent
    -controlled input reaching a filesystem write, so this must run
    *before* that path is built (and before anything is written), not be
    left to ``validate_node()``'s F01 check to catch after the fact."""
    if not note_id or note_id in (".", ".."):
        return False
    if "/" in note_id or "\\" in note_id or "\x00" in note_id:
        return False
    return True


# --- tool implementations ---------------------------------------------------
# Each takes (arguments, cwd) and returns a plain JSON-able dict. A dict
# containing an "error" key is the tool-level error convention every caller
# here shares (see dispatch()'s isError wiring below) - never an exception
# escaping to the transport loop.

def tool_search(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    query = str(args.get("query", ""))
    try:
        k = int(args.get("k", fetch.DEFAULT_K))
    except (TypeError, ValueError):
        return {"error": "k must be an integer"}
    start = args.get("root") or cwd

    # read_only=True: search is a read tool in every intent (see the module
    # docstring) - it must never leave a `.shapa-index.db`/`.shapa-vectors.json`
    # sidecar behind in a wiki it was only asked to search, which a plain
    # search call previously did as an undocumented side effect (fixed
    # here; shapa-backend-spec.md Slice 6 report).
    selection = fetch.select_multi(query, start=start, k=k, read_only=True)
    results = []
    for node, summary in selection.items:
        root_path = selection.item_roots.get(node.id)
        is_mem = fetch.is_memory(node)
        results.append({
            "id": node.id,
            "type": node.type,
            "source": "memory" if is_mem else "note",
            "kind": node.meta.get("kind") if is_mem else None,
            "root": str(root_path) if root_path is not None else None,
            # Memory v3: summaries only (progressive disclosure) - `get`
            # returns the full text of any id here.
            "summary": summary,
            "snippet": summary,
        })
    out: dict[str, Any] = {"results": results, "no_match": selection.no_match,
                           "mode": selection.mode}
    if selection.collisions:
        out["collisions"] = selection.collisions
    return out


def _db_lookup(root_path: Path, key: str) -> dict[str, Any] | None:
    """A format-4 row or ledger item by id or alias, with its tags and links."""
    from shapa import db

    if not db.exists(root_path):
        return None
    conn = db.connect(root_path)
    if conn is None:
        return None
    try:
        row = db.get_row(conn, key)
        if row is not None:
            return {"id": row.id, "type": db.KIND_NAMES[row.kind], "source": "row",
                    "meta": {"summary": row.summary, "alias": row.alias, "tags": row.tags,
                             "created": row.created, "origin": row.source},
                    "links": db.links_of(conn, row.id), "body": row.body}
        doc = db.get_doc(conn, key)
        if doc is not None:
            return {"id": doc.id, "type": "document", "source": "row",
                    "meta": {"summary": doc.title, "alias": doc.alias, "tags": doc.tags,
                             "created": doc.created, "mime": doc.mime, "origin": doc.source},
                    "links": db.links_of(conn, doc.id), "body": doc.body}
        item = db.get_item(conn, key)
        if item is not None:
            lines = []
            for depth, child in db.tree(conn, item.id)[1:]:
                lines.append("  " * depth + f"{child.id} [{child.status}] {child.title}")
            body = item.body + (("\n\n" if item.body else "") + "\n".join(lines) if lines else "")
            return {"id": item.id, "type": db.KIND_NAMES[item.kind], "source": "item",
                    "meta": {"summary": item.title, "status": item.status, "parent": item.parent,
                             "verify": item.verify, "git_ref": item.git_ref, "alias": item.alias},
                    "links": db.links_of(conn, item.id), "body": body}
    finally:
        conn.close()
    return None


def tool_get(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    note_id = str(args.get("id", "")).strip()
    if not note_id:
        return {"error": "id is required"}
    start = args.get("root") or cwd

    matches = []
    for wr in config.wiki_roots(start):
        root_path = Path(wr.path)
        if not root_path.is_dir():
            continue
        hit = _db_lookup(root_path, note_id)
        if hit is not None:
            matches.append({**hit, "root_kind": wr.kind, "root": str(wr.path)})
            continue
        if note_id.startswith(memlog.ID_PREFIX):
            hit = memlog.get(root_path, note_id)
            if hit is not None:
                rec, status = hit
                matches.append({"id": note_id, "type": "memory", "source": "memory",
                                "root_kind": wr.kind, "root": str(wr.path), "status": status,
                                "record": {f: getattr(rec, f) for f in memlog.FIELDS},
                                "body": rec.body})
                continue
        nodes = load_nodes(root_path)
        node = nodes.get(note_id)
        if node is None:
            continue
        body = frontmatter.parse(node.path).body
        matches.append({
            "id": note_id,
            "type": node.type,
            "root_kind": wr.kind,
            "root": str(wr.path),
            "meta": node.meta,
            "body": body,
        })

    if not matches:
        return {"error": f"no note '{note_id}' found in any wiki in scope"}
    if len(matches) > 1 and all(m.get("source") == "memory" for m in matches):
        return matches[0]  # content-derived id: the same memory in two roots
    if len(matches) > 1 and all(m.get("source") in ("row", "item") for m in matches):
        # Database ids are per wiki (R1 here, R1 in the global wiki): the most
        # specific wiki wins, the others are named so the caller can ask.
        return {**matches[0], "also_in": [m["root"] for m in matches[1:]]}
    if len(matches) > 1:
        # The F09 cross-root-collision contract (§4.1): never silently pick
        # one - surface every match and let the caller decide.
        return {
            "error": f"id '{note_id}' exists in more than one wiki root - ambiguous",
            "matches": matches,
        }
    return matches[0]


def tool_save(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    scope = str(args.get("scope", "")).strip()
    if scope not in VALID_SAVE_SCOPES:
        return {
            "error": f"scope must be one of {sorted(VALID_SAVE_SCOPES)} (got {scope!r}) - "
                     "'external' was retired (shapa-backend-spec.md §10 decision 4/6); "
                     "call the placement tool if unsure which to use, never guess",
        }

    summary = str(args.get("summary", "")).strip()
    if not summary:
        return {"error": "summary is required"}
    if "\n" in summary or len(summary) > MAX_SUMMARY_CHARS:
        return {"error": f"summary must be a single line, <={MAX_SUMMARY_CHARS} chars (F04)"}

    body = str(args.get("body", "")).strip()
    if not body:
        return {"error": "body is required"}

    note_type = str(args.get("type", "memory")).strip()
    if note_type not in VALID_NOTE_TYPES:
        return {"error": f"type must be one of {sorted(VALID_NOTE_TYPES)} (got {note_type!r})"}

    tags = args.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return {"error": "tags must be a list of strings"}

    start = args.get("root") or cwd
    if scope == "global":
        root = config.global_root()
    else:
        found = config.discover(start)
        if found is None:
            return {
                "error": "scope 'repo' requires a repo-local wiki, and none was found "
                         f"from {start!r} - run `shapa init` there first; a write never "
                         "creates one implicitly (shapa-backend-spec.md §10 decision 4)",
            }
        root = found

    from shapa import db, save as save_mod

    if db.exists(root):
        return save_mod.save_row(root, scope, note_type, summary, body, tags,
                                 alias=str(args.get("id") or "").strip(),
                                 project=str(args.get("project") or "").strip() or None,
                                 merge=bool(args.get("merge")))
    if note_type in save_mod.DB_ONLY_TYPES:
        return {"error": f"type {note_type!r} needs a format-4 wiki (a shapa.db) - "
                         f"run `shapa upgrade {root}` first"}

    note_id = str(args.get("id") or "").strip() or _slugify(summary)
    if not _is_safe_note_id(note_id):
        return {
            "error": f"invalid id {note_id!r} - ids may not contain '/', '\\\\', a NUL "
                     "byte, or be exactly '.' or '..' (path-traversal guard)",
        }
    path = root / f"{note_id}.md"
    if path.exists():
        return {
            "error": f"a note with id '{note_id}' already exists at {path} - "
                     "pass a different id, or use `get`/edit it directly to update it",
        }
    # A note by this id anywhere else in scope would be an F09 collision the
    # moment a read merges this root back in - refuse it here rather than
    # writing something a subsequent fetch/search will only later flag as
    # ambiguous.
    for wr in config.wiki_roots(start):
        other_root = Path(wr.path)
        if other_root.resolve() == root.resolve():
            continue
        if (other_root / f"{note_id}.md").exists():
            return {
                "error": f"id '{note_id}' already exists in the {wr.kind} wiki "
                         f"({wr.path}) - choose a different id to avoid an F09 "
                         "cross-root collision",
            }

    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_lines = [
        "---",
        f"id: {note_id}",
        f"type: {note_type}",
        f'created: "{now}"',
        "consequence: 5",
        "locus: output",
        "uses: 0",
        f'summary: "{summary}"',
        f"scope: {scope}",
    ]
    if tags:
        meta_lines.append("tags: [" + ", ".join(tags) + "]")
    meta_lines.append("status: active")
    meta_lines.append("---")
    path.write_text("\n".join(meta_lines) + "\n" + body + "\n", encoding="utf-8")

    result = validate_node(path)
    if not result.valid:
        path.unlink(missing_ok=True)
        errors = "; ".join(v.message for v in result.errors)
        return {"error": f"note failed validation and was not saved: {errors}"}

    out: dict[str, Any] = {"id": note_id, "path": str(path), "scope": scope}
    if result.warnings:
        out["warnings"] = [v.message for v in result.warnings]
    return out


def tool_placement(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    try:
        text = PLACEMENT_ASSET.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"could not read the bundled placement.md: {exc}"}
    return {"text": text}


def _scope_root(args: dict[str, Any], cwd: str | None, *, required: bool):
    """The wiki root a focus or skill tool writes to, or an error dict."""
    scope = str(args.get("scope") or "").strip()
    if scope not in VALID_SAVE_SCOPES:
        return None, {"error": f"scope must be one of {sorted(VALID_SAVE_SCOPES)} (got {scope!r})"}
    if scope == "global":
        return config.global_root(), None
    found = config.discover(args.get("root") or cwd)
    if found is None:
        return None, {"error": "scope 'repo' requires a repo-local wiki here - run `shapa init`"}
    return found, None


def tool_focus(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    from shapa import db, focus

    action = str(args.get("action") or "show").strip()
    if action == "show":
        out: dict[str, Any] = {}
        for wr in config.wiki_roots(args.get("root") or cwd):
            if wr.kind in ("global", "repo") and db.exists(wr.path):
                conn = db.connect(wr.path)
                try:
                    out[wr.kind] = focus.as_dict(conn)
                finally:
                    conn.close()
        return out
    if action not in ("northstar", "fire", "unfire", "order", "rank", "promote", "candidates"):
        return {"error": f"action must be show, northstar, fire, unfire, order, rank, promote "
                         f"or candidates (got {action!r})"}
    root, err = _scope_root(args, cwd, required=True)
    if err:
        return err
    if not db.exists(root):
        return {"error": f"{root} has no database - run `shapa upgrade {root}`"}
    conn = db.connect(root)
    try:
        try:
            if action == "northstar":
                focus.set_northstar(conn, str(args.get("text") or ""))
            elif action == "fire":
                if args.get("feature") and args.get("scope") == "global":
                    return {"error": "the global wiki has no ledger: a global fire is free text"}
                focus.set_fire(conn, args.get("slot"), feature=args.get("feature") or None,
                               text=args.get("text") or None)
            elif action == "unfire":
                focus.clear_fire(conn, args.get("slot"))
            elif action == "order":
                focus.reorder(conn, args.get("order") or [])
            elif action == "rank":
                rank = args.get("rank")
                focus.rank_feature(conn, str(args.get("feature") or ""),
                                   None if rank in (None, "", "none") else int(rank))
            elif action == "promote":
                focus.promote(conn, feature=args.get("feature") or None, slot=args.get("slot"))
            elif action == "candidates":
                return {"candidates": [{"id": i.id, "title": i.title, "rank": focus.rank_of(conn, i.id)}
                                       for i in focus.candidates(conn)]}
        except (db.LedgerError, ValueError, TypeError) as exc:
            return {"error": str(exc)}
        return focus.as_dict(conn)
    finally:
        conn.close()


def tool_skill(args: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    from shapa import db, skills

    action = str(args.get("action") or "list").strip()
    if action in ("list", "show"):
        out: list[Any] = []
        for wr in config.wiki_roots(args.get("root") or cwd):
            if wr.kind not in ("global", "repo") or not db.exists(wr.path):
                continue
            conn = db.connect(wr.path)
            try:
                if action == "list":
                    out += [{**s, "scope": wr.kind} for s in skills.summaries(conn)]
                else:
                    v = skills.get(conn, str(args.get("name") or ""),
                                   int(args["version"]) if args.get("version") else None)
                    if v is not None:
                        out.append({**skills.as_dict(v, body=True), "scope": wr.kind})
            except (ValueError, TypeError) as exc:
                return {"error": str(exc)}
            finally:
                conn.close()
        if action == "show" and not out:
            return {"error": f"no skill {args.get('name')!r} in any wiki in scope"}
        return {"skills": out}
    if action not in ("add", "update", "activate"):
        return {"error": f"action must be list, show, add, update or activate (got {action!r})"}
    root, err = _scope_root(args, cwd, required=True)
    if err:
        return err
    conn = db.connect(root) if db.exists(root) else None
    if conn is None:
        return {"error": f"{root} has no database - run `shapa upgrade {root}`"}
    try:
        try:
            name = str(args.get("name") or "")
            if action == "activate":
                v = skills.activate(conn, name, int(args["version"]) if args.get("version") else None)
            else:
                v = skills.add_version(conn, name, str(args.get("description") or ""),
                                       str(args.get("body") or ""), new=action == "add",
                                       active=bool(args.get("active")) or action == "add",
                                       source="mcp")
        except (db.LedgerError, ValueError, TypeError) as exc:
            return {"error": str(exc)}
        return skills.as_dict(v)
    finally:
        conn.close()


_HANDLERS: dict[str, Callable[[dict[str, Any], str | None], dict[str, Any]]] = {
    "search": tool_search,
    "get": tool_get,
    "save": tool_save,
    "placement": tool_placement,
    "focus": tool_focus,
    "skill": tool_skill,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Search every shapa wiki in scope (the global wiki plus this "
            "repo's own, if it has one) for md notes and captured memory "
            "records relevant to a query, in one fused ranking. Returns ids "
            "and one-line summaries - the same merge the per-prompt fetch "
            "hook uses - plus the retrieval mode (fused or bm25); call `get` "
            "for an id's full text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search text."},
                "k": {"type": "integer", "description": "Max results (default 8)."},
                "root": {
                    "type": "string",
                    "description": "Directory to resolve wikis from (default: this server's cwd).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get",
        "description": (
            "Fetch the full text of one note or memory record by id (a "
            "memory id starts with 'm-'), searching every wiki in scope. "
            "Errors, rather than guessing, if a note id exists in more than "
            "one wiki root, or in none."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The note's id (filename stem)."},
                "root": {"type": "string", "description": "Directory to resolve wikis from."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "save",
        "description": (
            "Write a new note. `scope` is required and is exactly 'global' "
            "(true in any repo) or 'repo' (true only in this checkout - "
            "requires an existing repo-local wiki; run `shapa init` first if "
            "there isn't one). Never guesses a scope - call `placement` "
            "first if unsure which one applies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["global", "repo"]},
                "summary": {
                    "type": "string",
                    "description": "One line, <=160 chars - the only thing session-start bootstrap loads.",
                },
                "body": {"type": "string", "description": "The note's full body."},
                "type": {
                    "type": "string",
                    "enum": ["memory", "rule", "issue", "document", "idea"],
                    "default": "memory",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "id": {
                    "type": "string",
                    "description": "Optional explicit id; slugified from `summary` otherwise.",
                },
                "project": {
                    "type": "string",
                    "description": "document only: the project or subproject key (default: this "
                                   "wiki's own). A project keeps ONE document; a second is refused "
                                   "with the first one's id unless `merge` is true.",
                },
                "merge": {
                    "type": "boolean",
                    "description": "document only: append this one as a section of the project's "
                                   "existing document instead of refusing it.",
                },
                "root": {
                    "type": "string",
                    "description": "Directory to resolve 'repo' scope from (default: this server's cwd).",
                },
            },
            "required": ["scope", "summary", "body"],
        },
    },
    {
        "name": "placement",
        "description": (
            "The decision rule for whether a new note belongs in the global "
            "wiki or this repo's own wiki. Call this before `save` if unsure."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "focus",
        "description": (
            "The northstar and the three ranked big fires of the global wiki and this "
            "repo's own. action `show` (default) reads both; `northstar` {text}, "
            "`fire` {slot 1-3, feature | text}, `unfire` {slot}, `order` {order: [2,1,3]}, "
            "`rank` {feature, rank}, `promote` {feature?, slot?}, `candidates` set or move "
            "them and need `scope` (global or repo). A global fire is free text; a repo "
            "fire points at a ledger feature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "northstar", "fire", "unfire",
                                                      "order", "rank", "promote", "candidates"]},
                "scope": {"type": "string", "enum": ["global", "repo"]},
                "text": {"type": "string"}, "slot": {"type": "integer"},
                "feature": {"type": "string"}, "rank": {"type": "integer"},
                "order": {"type": "array", "items": {"type": "integer"}},
                "root": {"type": "string"},
            },
        },
    },
    {
        "name": "skill",
        "description": (
            "Skills stored as versioned records, global or per repo. action `list` (default) "
            "and `show` {name, version?} read them; `add` {name, description, body} makes "
            "version 1 active; `update` makes a new pending version (`active: true` "
            "activates it at once); `activate` {name, version?} activates the newest "
            "pending one. add, update and activate need `scope`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "show", "add", "update", "activate"]},
                "scope": {"type": "string", "enum": ["global", "repo"]},
                "name": {"type": "string"}, "description": {"type": "string"},
                "body": {"type": "string"}, "version": {"type": "integer"},
                "active": {"type": "boolean"}, "root": {"type": "string"},
            },
        },
    },
]


# --- JSON-RPC 2.0 dispatch (the minimal MCP surface) ------------------------

def dispatch(request: dict[str, Any], cwd: str | None) -> dict[str, Any] | None:
    """Handle one JSON-RPC 2.0 request/notification.

    Returns the response object, or ``None`` for a notification (a message
    with no ``id`` - the JSON-RPC spec says notifications get no reply,
    e.g. the client's ``notifications/initialized``)."""
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any] | None:
        if req_id is None:
            return None  # never answer a malformed/erroring notification
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(str(name))
        if handler is None:
            return err(-32602, f"unknown tool '{name}'")
        args = params.get("arguments") or {}
        try:
            payload = handler(args, cwd)
        except Exception as exc:  # a tool must never take the server down
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        is_error = isinstance(payload, dict) and "error" in payload
        return ok({
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error,
        })

    return err(-32601, f"unknown method '{method}'")


def serve_stdio(stdin=None, stdout=None, cwd: str | None = None) -> None:
    """Run the line-delimited JSON-RPC loop until stdin closes.

    Never blocks on a malformed line (skipped, not a crash) and never lets
    one tool's exception take the server down - matching every other
    hook/CLI entry point in this codebase's never-block contract."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(request, dict):
            continue
        response = dispatch(request, cwd)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m shapa.mcp",
        description="Run the shapa MCP stdio server (search/get/save/placement tools).",
    )
    parser.add_argument(
        "--cwd", default=None,
        help="Directory to resolve wikis from (default: this process's own cwd).",
    )
    args = parser.parse_args(argv)
    serve_stdio(cwd=args.cwd)


if __name__ == "__main__":
    main()
