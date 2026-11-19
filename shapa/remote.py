"""``shapa remote`` - the opt-in hosted store (``.shapa/arch/hosted.md``).

::

    shapa remote login URL [--device NAME] [--scopes read,write,delete] [--token-stdin]
    shapa remote sync [--cwd DIR] [--all] [--key KEY] [--hook] [--if-changed] [--dry-run] [--json]
    shapa remote status [--check] [--json]
    shapa remote devices [--revoke ID]
    shapa remote devices --mint NAME --env-file PATH --env-var VAR [--reach baseline|full]
                         [--wikis KEYS] [--scopes read,write]
    shapa remote logout
    shapa remote instructions [--client NAME] [--full]

The login lives in ``~/.shapa`` (``$SHAPA_REMOTE_DIR``): ``remote.json`` (url,
device name, device id) and ``remote.token`` (mode 0600). The owner key comes
from ``$SHAPA_OWNER_KEY`` or a hidden prompt - never from argv, never stored,
never printed. Nothing here is needed unless a remote is configured; the engine
works unchanged without one.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from shapa import db, remotesync, sync, syncstate
from shapa.remotehttp import Client, RemoteError, check_url, clean_text

OWNER_KEY_ENV = "SHAPA_OWNER_KEY"
SCOPES = ("read", "write", "delete")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOKEN_RE = re.compile(r"^shp_([A-Za-z0-9-]{1,64})_[A-Za-z0-9_-]{8,200}$")
_CLIENT_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

#: Tests inject an in-process transport here; ``None`` means real HTTP.
TRANSPORT = None


class UsageError(Exception):
    pass


def _owner_key() -> str:
    key = os.environ.get(OWNER_KEY_ENV) or getpass.getpass("Owner key: ")
    if not key or not key.strip():
        raise UsageError(f"an owner key is required (${OWNER_KEY_ENV} or the prompt)")
    return key.strip()


def _device_name() -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", socket.gethostname()).strip(" -.")
    return name[:40] or "device"


def _creds():
    creds = syncstate.load_credentials()
    if creds is None:
        raise UsageError("not logged in - run `shapa remote login URL`")
    return creds


def _client(creds, **kw) -> Client:
    return Client(creds.url, creds.token, transport=TRANSPORT, **kw)


# --- login / logout --------------------------------------------------------------------------

def _parse_scopes(text: str) -> list[str]:
    scopes = [s for s in re.split(r"[,\s]+", text) if s]
    if not scopes or any(s not in SCOPES for s in scopes):
        raise UsageError(f"--scopes must be a list drawn from {', '.join(SCOPES)}")
    return sorted(set(scopes))


def cmd_login(args) -> int:
    url = check_url(args.url)
    device = clean_text(args.device or _device_name(), 40)
    if args.token_stdin:
        token = sys.stdin.read().strip()
        m = _TOKEN_RE.match(token)
        if not m:
            raise UsageError("the token on stdin is not a device token (shp_<id>_<secret>)")
        who = Client(url, token, transport=TRANSPORT).request("GET", "/v1/whoami", timeout=10)
        device_id = who.get("id") if isinstance(who.get("id"), str) else m.group(1)
        device = clean_text(who.get("name") or device, 40)
    else:
        scopes = _parse_scopes(args.scopes)
        owner = Client(url, _owner_key(), transport=TRANSPORT)
        minted = owner.request("POST", "/admin/devices", {"name": device, "scopes": scopes},
                               timeout=15)
        token, device_id = minted.get("token"), minted.get("id")
        if not (isinstance(token, str) and _TOKEN_RE.match(token) and isinstance(device_id, str)
                and _ID_RE.match(device_id)):
            raise RemoteError("the server did not return a usable device token")
    syncstate.save_credentials(syncstate.Credentials(url, device, device_id, token))
    print(f"logged in as {device} ({device_id}) at {url}")
    return 0


def cmd_logout(args) -> int:
    gone = syncstate.clear_credentials()
    print("logged out (the device stays registered on the server; revoke it with "
          "`shapa remote devices --revoke ID`)" if gone else "not logged in")
    return 0


# --- sync -------------------------------------------------------------------------------------

def _print_result(r: sync.SyncResult) -> None:
    if r.status in ("ok", "unchanged"):
        if r.dry_run:
            print(f"{r.key}: would send {r.sent_put} record(s) and {r.sent_del} delete(s), "
                  f"re-fetch {r.wanted}; the server has {r.received} record(s) past the cursor")
        elif r.status == "unchanged":
            print(f"{r.key}: nothing changed locally")
        else:
            extra = "".join(f", {n} {label}" for n, label in
                            ((r.renamed, "renamed"), (r.skipped, "skipped"), (r.invalid, "ignored"))
                            if n)
            print(f"{r.key}: sent {r.sent_put} record(s) and {r.sent_del} delete(s), "
                  f"received {r.received}, applied {r.applied}{extra}")
        for rid, message in r.refused[:10]:
            print(f"{r.key}: the server refused {rid}: {message}", file=sys.stderr)
        for rid, message in r.notes[:10]:
            print(f"{r.key}: note on {rid}: {message}")
        if r.oversized:
            print(f"{r.key}: skipped {len(r.oversized)} record(s) over the server limit of 1 MB: "
                  f"{', '.join(r.oversized[:10])}", file=sys.stderr)
        if r.notes_skipped:
            print(f"{r.key}: note files skipped - this checkout is not on its default branch "
                  "(database records still synced)", file=sys.stderr)
    else:
        print(f"{r.key}: {r.status}" + (f" - {r.message}" if r.message else ""), file=sys.stderr)


def cmd_sync(args) -> int:
    creds = syncstate.load_credentials()
    if args.hook:
        return remotesync.run_hook(args, creds, transport=TRANSPORT)
    if creds is None:
        raise UsageError("not logged in - run `shapa remote login URL`")
    if args.key is not None and (args.all or not remotesync.KEY_RE.match(args.key)):
        raise UsageError("--key must be a lowercase slug and cannot be combined with --all")
    roots = remotesync.wikis_in_scope(args.cwd, args.all)
    if not roots:
        raise UsageError("no wiki in scope (run `shapa init`, or use --all)")
    results = remotesync.run_sync(_client(creds), syncstate.remote_dir(), roots, key=args.key,
                                  dry_run=args.dry_run, if_changed=args.if_changed)
    if args.json:
        print(json.dumps({"wikis": [r.as_dict() for r in results]}, indent=2))
    else:
        for r in results:
            _print_result(r)
    return 1 if any(r.status in ("error", "busy", "needs-key") for r in results) else 0


# --- status / devices / instructions -----------------------------------------------------------

def cmd_status(args) -> int:
    creds = syncstate.load_credentials()
    out: dict = {"logged_in": creds is not None, "wikis": []}
    code = 0
    if creds is None:
        code = 1 if args.check else 0
    else:
        out.update(url=creds.url, device=creds.device, device_id=creds.device_id)
        rdir = syncstate.remote_dir()
        for root in remotesync.wikis_in_scope(args.cwd, args.all):
            key = remotesync.wiki_key(root, rdir=rdir)
            if key is None:
                out["wikis"].append({"key": None, "name": remotesync.folder_name(root),
                                     "path": str(root), "pending": None, "last_sync": None,
                                     "needs_key": True})
                continue
            pending = sync.pending_changes(root, key=key, url=creds.url, rdir=rdir)
            state = syncstate.load_state(syncstate.state_path(rdir, db.db_path(root)), key, creds.url)
            out["wikis"].append({"key": key, "path": str(root), "pending": pending,
                                 "last_sync": state.get("last_sync") or None})
        if args.check:
            try:
                _client(creds).request("GET", "/v1/whoami", timeout=5)
                out["server"] = "ok"
            except RemoteError as exc:
                out["server"], out["error"], code = "unreachable", clean_text(exc), 1
    if args.json:
        print(json.dumps(out, indent=2))
        return code
    if creds is None:
        print("not logged in")
        return code
    print(f"logged in as {creds.device} ({creds.device_id}) at {creds.url}")
    for w in out["wikis"]:
        if w.get("needs_key"):
            print(f"  {w['name']}: needs --key (no git origin; run `shapa remote sync --key KEY` once)")
            continue
        pending = "no database" if w["pending"] is None else f"{w['pending']} pending"
        print(f"  {w['key']}: {pending}, last sync {w['last_sync'] or 'never'}")
    if args.check:
        print(f"server: {out['server']}" + (f" ({out['error']})" if out.get("error") else ""))
    return code


_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_WIKI_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,127}$")
REACHES = ("full", "baseline")


def _git_out(folder, *argv) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", *argv], cwd=folder, capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _check_env_file(path: Path) -> Path:
    """The file a minted token goes into: not a symlink, not a directory, not tracked by git.
    Raises :class:`UsageError`."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    if path.is_symlink():
        raise UsageError("--env-file is a symlink: refusing to write a token through it")
    if path.exists() and not path.is_file():
        raise UsageError("--env-file is not a regular file")
    folder = path.parent
    if not folder.is_dir():
        raise UsageError("the folder of --env-file does not exist")
    tracked = _git_out(folder, "ls-files", "--error-unmatch", "--", path.name)
    if tracked is not None and tracked.returncode == 0:
        raise UsageError("--env-file is tracked by git: a token must never be committed "
                         "(untrack it, or choose an ignored file)")
    return path


def _set_env_line(existing: str, var: str, value: str) -> str:
    """*existing* with the ``VAR=`` line replaced (every other line untouched), or one appended."""
    lines = existing.splitlines(keepends=True)
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(var)}\s*=")
    new = f"{var}={value}\n"
    done = False
    out = []
    for line in lines:
        if pat.match(line):
            if not done:
                out.append(new)
                done = True
            continue  # a repeated assignment of the same variable is dropped
        out.append(line)
    if not done:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(new)
    return "".join(out)


def _write_env_file(path: Path, var: str, token: str) -> None:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError):
        raise UsageError("--env-file cannot be read as text") from None
    syncstate.atomic_write(path, _set_env_line(existing, var, token), 0o600)


def cmd_mint(args, admin: Client) -> int:
    """Mint a device token and put it in an env file; the token is printed nowhere."""
    name, var = args.mint, args.env_var
    if not (isinstance(name, str) and 1 <= len(name) <= 64 and name == name.strip()
            and not re.search(r"[\x00-\x1f\x7f]", name)):
        raise UsageError("--mint NAME: 1-64 characters, no control characters, no surrounding spaces")
    if not args.env_file or not var:
        raise UsageError("--mint needs --env-file PATH and --env-var VAR (the token is never printed)")
    if not _ENV_VAR_RE.match(var):
        raise UsageError("--env-var must be a shell variable name")
    scopes = _parse_scopes(args.scopes)
    wikis = None
    if args.wikis and args.wikis != "all":
        wikis = [w for w in re.split(r"[,\s]+", args.wikis) if w]
        if not wikis or any(not _WIKI_RE.match(w) for w in wikis):
            raise UsageError("--wikis is a comma list of wiki keys, or `all`")
    path = _check_env_file(Path(args.env_file))  # before the server is asked: no token without a place for it
    body = {"name": name, "scopes": scopes, "reach": args.reach, "wikis": wikis}
    minted = admin.request("POST", "/admin/devices", body, timeout=15)
    token, device_id = minted.get("token"), minted.get("id")
    if not (isinstance(token, str) and _TOKEN_RE.match(token) and isinstance(device_id, str)
            and _ID_RE.match(device_id)):
        raise RemoteError("the server did not return a usable device token")
    try:
        _write_env_file(path, var, token)
    except (OSError, UsageError):
        try:  # no token is kept anywhere: the device is of no use, so it is revoked again
            admin.request("DELETE", f"/admin/devices/{device_id}", timeout=15)
        except RemoteError:
            pass
        raise UsageError(f"could not write {path}; the new device {device_id} was revoked") from None
    reach = minted.get("reach") if minted.get("reach") in REACHES else args.reach
    print(f"minted {clean_text(name, 64)} ({device_id}), reach {reach}: {var} written to {path}")
    return 0


def cmd_devices(args) -> int:
    creds = syncstate.load_credentials()
    url = check_url(args.url) if args.url else (creds.url if creds else None)
    if url is None:
        raise UsageError("not logged in - pass --url URL, or run `shapa remote login URL`")
    if args.mint is None and (args.env_file or args.env_var):
        raise UsageError("--env-file and --env-var go with --mint")
    admin = Client(url, _owner_key(), transport=TRANSPORT)
    if args.mint is not None:
        return cmd_mint(args, admin)
    if args.revoke:
        if not _ID_RE.match(args.revoke):
            raise UsageError("not a device id")
        admin.request("DELETE", f"/admin/devices/{args.revoke}", timeout=15)
        print(f"revoked {args.revoke}")
        return 0
    listing = admin.request("GET", "/admin/devices", timeout=15).get("devices")
    if not isinstance(listing, list):
        raise RemoteError("unexpected device list from the server")
    for d in listing:
        if isinstance(d, dict):
            scopes = ",".join(str(s) for s in d.get("scopes", []) if isinstance(s, str))
            state = " revoked" if d.get("revoked") else ""
            reach = f" {clean_text(d['reach'], 10)}" if d.get("reach") in REACHES else ""
            print(f"{clean_text(d.get('id', '?'), 64)}  {clean_text(d.get('name', ''), 60)}  "
                  f"{scopes}{reach}{state}")
    return 0


def cmd_instructions(args) -> int:
    if not _CLIENT_RE.match(args.client):
        raise UsageError("--client must be a short lowercase name such as mcp or rest")
    text = _client(_creds()).request(
        "GET", f"/v1/instructions?client={args.client}&budget={int(args.budget)}"
        + ("&full=1" if getattr(args, "full", False) else ""), accept="text/plain")
    print(text.rstrip("\n"))
    return 0


# --- entry point -------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shapa remote", description="The opt-in hosted store.")
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="register this machine as a device")
    lg.add_argument("url")
    lg.add_argument("--device", default=None, help="device name (default: this host's name)")
    lg.add_argument("--scopes", default=",".join(SCOPES))
    lg.add_argument("--token-stdin", action="store_true",
                    help="read an already minted device token from stdin instead of the owner key")
    sy = sub.add_parser("sync", help="two-way sync of every wiki in scope")
    sy.add_argument("--cwd", default=None)
    sy.add_argument("--all", action="store_true", help="every registered wiki, not just this repo's")
    sy.add_argument("--key", default=None, help="override this repo wiki's key")
    sy.add_argument("--hook", action="store_true", help="harness hook: stdin JSON, 8 s, never fails")
    sy.add_argument("--if-changed", action="store_true", help="skip the network when nothing changed here")
    sy.add_argument("--dry-run", action="store_true")
    sy.add_argument("--json", action="store_true")
    st = sub.add_parser("status", help="login and pending changes")
    st.add_argument("--cwd", default=None)
    st.add_argument("--all", action="store_true")
    st.add_argument("--check", action="store_true", help="exit 1 if not logged in or unreachable")
    st.add_argument("--json", action="store_true")
    dv = sub.add_parser("devices", help="list, revoke or mint devices (needs the owner key)")
    dv.add_argument("--revoke", default=None, metavar="ID")
    dv.add_argument("--url", default=None)
    dv.add_argument("--mint", default=None, metavar="NAME",
                    help="mint a device token and write it to --env-file as --env-var (never printed)")
    dv.add_argument("--reach", default="baseline", choices=REACHES,
                    help="with --mint: baseline reads stage 0 and writes notes, tasks, ideas, "
                         "decisions and files (default); full is a device like this machine")
    dv.add_argument("--wikis", default=None, metavar="KEYS",
                    help="with --mint: comma list of wiki keys the device may use (default: all)")
    dv.add_argument("--scopes", default="read,write", help="with --mint (default read,write)")
    dv.add_argument("--env-file", default=None, metavar="PATH",
                    help="with --mint: the file that gets VAR=<token> (mode 0600; not a symlink, "
                         "not tracked by git; other lines are kept)")
    dv.add_argument("--env-var", default=None, metavar="VAR", help="with --mint: the variable name")
    sub.add_parser("logout", help="forget the stored login")
    ins = sub.add_parser("instructions", help="print the server's session defaults")
    ins.add_argument("--client", default="mcp")
    ins.add_argument("--budget", type=int, default=6000)
    ins.add_argument("--full", action="store_true",
                     help="also the rules and the other live features (default: the northstar, "
                          "the three fires and their tree)")
    return p


_COMMANDS = {"login": cmd_login, "sync": cmd_sync, "status": cmd_status, "devices": cmd_devices,
             "logout": cmd_logout, "instructions": cmd_instructions}


def run(argv: list[str]) -> int:
    if "--hook" in argv and argv[:1] == ["sync"]:
        # A hook must never fail the harness - not even on a bad flag.
        try:
            args = build_parser().parse_args(argv)
            return cmd_sync(args)
        except BaseException:
            return 0
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except (UsageError, RemoteError) as exc:
        print(f"shapa remote: {clean_text(exc, 300)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
