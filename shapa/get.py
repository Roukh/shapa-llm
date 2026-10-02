"""``shapa get <id>`` - the full text behind any id a prompt surfaced.

Progressive disclosure (memory v3): the per-prompt hook and session start
inject one-line summaries only; this returns the whole md note body, or a
memory record's full text with its metadata. Same lookup as the MCP
``get`` tool (:func:`shapa.mcp.tool_get`): every wiki in scope, an
ambiguous note id reported rather than guessed.

CLI::

    shapa get ID [--cwd DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from shapa import mcp


def render(result: dict) -> str:
    if result.get("source") == "memory":
        rec = result["record"]
        tags = f" tags: {', '.join(rec['tags'])}" if rec.get("tags") else ""
        sup = f" supersedes: {rec['supersedes']}" if rec.get("supersedes") else ""
        return (f"{rec['id']} ({rec['kind']}, {result['status']}, {rec['scope']}"
                f"{', ' + rec['repo'] if rec.get('repo') else ''}) created {rec['created']}"
                f" source: {rec['source'] or '-'}{tags}{sup}\n{rec['summary']}\n\n{rec['body']}")
    meta = result.get("meta") or {}
    summary = f"\n{meta['summary']}" if meta.get("summary") else ""
    return f"{result['id']} ({result['type']}) {result['root']}{summary}\n\n{result['body']}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shapa get",
                                     description="Print the full text of a note or memory by id.")
    parser.add_argument("id", help="A note id, or a memory id (m-...).")
    parser.add_argument("--cwd", default=None, help="Directory to resolve wikis from.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)
    result = mcp.tool_get({"id": args.id}, args.cwd)
    if "error" in result:
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"shapa get: {result['error']}", file=sys.stderr)
            for m in result.get("matches", []):
                print(f"  - {m['root_kind']}: {m['root']}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2, default=str) if args.json else render(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
