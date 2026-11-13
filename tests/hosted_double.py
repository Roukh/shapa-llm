"""An in-process reference double of the hosted store (test only).

It implements the contract in ``.shapa/arch/hosted.md`` - the sync op table, the
auth routes the client uses - behind the same ``transport(method, url, headers,
body, timeout)`` signature the client's HTTP layer takes, so tests need no
socket. The real server is tested separately; this double only has to agree
with the contract.
"""

import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

OWNER_KEY = "o" * 40
MAX_OPS = 200
MAX_RECORD_BYTES = 1_000_000
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_WANT = 500
PAGE_BYTES = 8_000_000


class Wiki:
    def __init__(self, key, name="", kind="repo", origin=""):
        self.key, self.name, self.kind, self.origin = key, name, kind, origin
        self.records = {}   # id -> wire record (with rev, deleted, device)
        self.seq = 0
        self.counters = {}  # kind -> next number

    def next_rev(self):
        self.seq += 1
        return self.seq

    def bump(self, kind, n):
        self.counters[kind] = max(self.counters.get(kind, 1), n)

    def fresh_id(self, kind):
        n = self.counters.get(kind, 1)
        while f"{kind}{n}" in self.records:
            n += 1
        self.counters[kind] = n + 1
        return f"{kind}{n}"


class HostedDouble:
    def __init__(self, *, page_size=500, page_bytes=PAGE_BYTES):
        self.page_bytes = page_bytes
        self.refuse = None     # fn(record) -> error message: the server refuses that record
        self.wikis = {}
        self.devices = {}      # id -> {name, scopes, token, revoked}
        self.page_size = page_size
        self.calls = []        # (method, path) of every request, for assertions
        self.sync_requests = []
        self.lock = threading.Lock()
        self.hang = None       # a threading.Event the next request waits on
        self.mutate_response = None  # fn(path, payload) -> payload, to play hostile server
        self.redirect_to = None
        self.on_sync = None    # fn(key, request) called before a sync request is processed

    # -- transport ---------------------------------------------------------------------
    def transport(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        path = parts.path
        self.calls.append((method, path))
        if self.hang is not None:
            self.hang.wait(30)
        if self.redirect_to:
            return 302, {"location": self.redirect_to}, b""
        status, payload = self.route(method, path, parse_qs(parts.query), headers, body)
        if self.mutate_response is not None and isinstance(payload, dict):
            payload = self.mutate_response(path, payload)
        if isinstance(payload, str):
            return status, {"content-type": "text/plain"}, payload.encode()
        return status, {"content-type": "application/json"}, json.dumps(payload).encode()

    # -- routing -----------------------------------------------------------------------
    def route(self, method, path, query, headers, body):
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
        bearer = auth[7:] if auth.startswith("Bearer ") else ""
        if path == "/health":
            return 200, {"ok": True}
        if path.startswith("/admin/"):
            if bearer != OWNER_KEY:
                return 401, self.err("unauthorized", "bad owner key")
            return self.admin(method, path, body)
        device = self.device_for(bearer)
        if device is None:
            return 401, self.err("unauthorized", "bad token")
        if path == "/v1/whoami":
            return 200, {"id": device["id"], "name": device["name"], "scopes": device["scopes"]}
        if path == "/v1/instructions":
            return 200, "Session defaults for " + (query.get("client") or ["rest"])[0]
        m = re.fullmatch(r"/v1/wikis/([^/]+)", path)
        if m and method == "PUT":
            return self.put_wiki(m.group(1), json.loads(body or b"{}"))
        m = re.fullmatch(r"/v1/wikis/([^/]+)/sync", path)
        if m and method == "POST":
            return self.sync(m.group(1), device, json.loads(body or b"{}"))
        return 404, self.err("not_found", path)

    @staticmethod
    def err(code, message):
        return {"error": {"code": code, "message": message}}

    def device_for(self, bearer):
        for d in self.devices.values():
            if d["token"] == bearer and not d["revoked"]:
                return d
        return None

    def admin(self, method, path, body):
        if path == "/admin/devices" and method == "POST":
            req = json.loads(body)
            did = f"d{len(self.devices) + 1}"
            token = f"shp_{did}_" + "s3cr3t" * 6
            self.devices[did] = {"id": did, "name": req["name"], "scopes": req["scopes"],
                                 "token": token, "revoked": False}
            return 200, {"id": did, "token": token}
        if path == "/admin/devices" and method == "GET":
            return 200, {"devices": [{k: v for k, v in d.items() if k != "token"}
                                     for d in self.devices.values()]}
        m = re.fullmatch(r"/admin/devices/([^/]+)", path)
        if m and method == "DELETE":
            if m.group(1) not in self.devices:
                return 404, self.err("not_found", "no such device")
            self.devices[m.group(1)]["revoked"] = True
            return 200, {"ok": True}
        return 404, self.err("not_found", path)

    def put_wiki(self, key, req):
        wiki = self.wikis.get(key)
        if wiki is None:
            self.wikis[key] = Wiki(key, req.get("name", ""), req.get("kind", "repo"),
                                   req.get("origin", ""))
        elif req.get("origin") and wiki.origin and req["origin"] != wiki.origin:
            return 409, self.err("conflict", "origin differs")
        elif req.get("origin"):
            wiki.origin = req["origin"]
        return 200, {"key": key}

    # -- the sync op table -------------------------------------------------------------
    def sync(self, key, device, req):
        wiki = self.wikis.get(key)
        if wiki is None:
            return 404, self.err("not_found", "no such wiki")
        ops = req.get("ops") or []
        if len(ops) > MAX_OPS:
            return 400, self.err("invalid", "too many ops")
        if len(json.dumps(req)) > MAX_REQUEST_BYTES or len(req.get("want") or []) > MAX_WANT:
            return 413, self.err("too_large", "request too large")
        for op in ops:
            if op["op"] == "put" and len(json.dumps(op["record"], separators=(",", ":"))) > MAX_RECORD_BYTES:
                return 413, self.err("too_large", "record too large")
        self.sync_requests.append((key, req))
        if self.on_sync is not None:
            self.on_sync(key, req)
        for kind, n in (req.get("counters") or {}).items():
            wiki.bump(kind, int(n))
        results, rejected, renames = [], [], {}
        for op in ops:
            if op["op"] == "del" and "delete" not in device["scopes"]:
                res = {"id": op["id"], "status": "rejected", "rev": 0,
                       "error": "this device lacks the delete scope"}
            else:
                res = self.apply_op(wiki, device, op, renames)
            results.append(res)
            if res["status"] == "rejected":
                rejected.append(res.get("new_id") or (op["record"]["id"] if op["op"] == "put" else op["id"]))
        cursor = int(req.get("cursor") or 0)
        pending = sorted((r for r in wiki.records.values() if r["rev"] > cursor),
                         key=lambda r: r["rev"])
        page, size = [], 0
        for r in pending:  # a page holds at most page_size records and page_bytes of JSON
            n = len(json.dumps(r))
            if page and (len(page) >= self.page_size or size + n > self.page_bytes):
                break
            page.append(r)
            size += n
        more = len(page) < len(pending)
        extra = [wiki.records[i] for i in list(req.get("want") or []) + rejected
                 if i in wiki.records and wiki.records[i] not in page]
        out_cursor = page[-1]["rev"] if more else wiki.seq
        return 200, {"cursor": out_cursor, "results": results, "changes": page + extra,
                     "counters": dict(wiki.counters), "more": more}

    def apply_op(self, wiki, device, op, renames):
        if op["op"] == "put":
            return self.apply_put(wiki, device, op, renames)
        return self.apply_del(wiki, device, op)

    def apply_put(self, wiki, device, op, renames):
        rec = json.loads(json.dumps(op["record"]))
        orig = rec["id"]
        # a rename earlier in this request rewrites later references to the old id
        data = rec.get("data") or {}
        if data.get("parent") in renames:
            data["parent"] = renames[data["parent"]]
        rec["links"] = [[renames.get(dst, dst), rel] for dst, rel in rec.get("links") or []]
        base_rev = op.get("base_rev")
        if self.refuse is not None and (msg := self.refuse(rec)):
            return {"id": orig, "status": "rejected", "rev": 0, "error": msg}
        if rec["kind"] == "N":  # a note path is its identity
            twin = wiki.records.get(orig)
        else:
            twin = next((r for r in wiki.records.values() if r["uid"] == rec["uid"]), None)
        if twin is not None:  # the uid already lives here, maybe under another id
            res = self.same_uid(wiki, device, twin, rec, base_rev)
            if twin["id"] != orig:
                res["new_id"] = twin["id"]
                renames[orig] = twin["id"]
            return {**res, "id": orig}
        cur = wiki.records.get(orig)
        if cur is None:
            return self.store(wiki, device, rec, "accepted", orig)
        new = wiki.fresh_id(rec["kind"])  # the id is held by another uid
        renames[orig] = new
        rec["id"] = new
        res = self.store(wiki, device, rec, "renamed", orig)
        res["new_id"] = new
        return res

    def same_uid(self, wiki, device, cur, rec, base_rev):
        rec = {**rec, "id": cur["id"]}
        if not cur["deleted"] and self.same_content(cur, rec):
            return {"status": "noop", "rev": cur["rev"]}
        if base_rev is not None and base_rev == cur["rev"]:
            return self.store(wiki, device, rec, "accepted", cur["id"])
        if rec["kind"] == "N" and base_rev is None and not cur["deleted"]:
            return {"status": "rejected", "rev": cur["rev"]}  # a file mtime is not an edit time
        if rec["updated"] > cur["updated"]:
            return self.store(wiki, device, rec, "accepted", cur["id"])
        return {"status": "rejected", "rev": cur["rev"]}

    @staticmethod
    def same_content(cur, rec):
        return all(cur.get(f) == rec.get(f) for f in ("data", "tags", "links", "updated"))

    def store(self, wiki, device, rec, status, orig):
        rev = wiki.next_rev()
        stored = {"id": rec["id"], "uid": rec.get("uid", ""), "kind": rec["kind"],
                  "created": rec["created"],
                  "updated": rec["updated"], "rev": rev, "deleted": False,
                  "device": device["name"], "data": rec.get("data") or {},
                  "tags": sorted(set(rec.get("tags") or [])),
                  "links": sorted(rec.get("links") or [])}
        wiki.records[rec["id"]] = stored
        m = re.fullmatch(r"([A-Z])(\d+)", rec["id"])
        if m:
            wiki.bump(m.group(1), int(m.group(2)) + 1)
        return {"id": orig, "status": status, "rev": rev}

    def apply_del(self, wiki, device, op):
        # the record is found by uid, under any id
        cur = next((r for r in wiki.records.values()
                    if r["uid"] == op["uid"] and not r["deleted"] and r["kind"] != "N"), None)
        if cur is None:
            return {"id": op["id"], "status": "noop", "rev": 0}
        if op.get("base_rev") == cur["rev"] or cur["updated"] <= op["deleted_at"]:
            cur["deleted"], cur["rev"], cur["device"] = True, wiki.next_rev(), device["name"]
            return {"id": op["id"], "status": "accepted", "rev": cur["rev"]}
        return {"id": op["id"], "status": "rejected", "rev": cur["rev"]}

    # -- test helpers ------------------------------------------------------------------
    def add_device(self, name="dev", scopes=("read", "write", "delete")):
        did = f"d{len(self.devices) + 1}"
        token = f"shp_{did}_" + "s3cr3t" * 6
        self.devices[did] = {"id": did, "name": name, "scopes": list(scopes), "token": token,
                             "revoked": False}
        return did, token

    def remote_delete(self, key, record_id):
        """Play a delete made through the REST API: tombstone the record."""
        wiki = self.wikis[key]
        rec = wiki.records[record_id]
        rec["deleted"], rec["rev"] = True, wiki.next_rev()

    def remote_put(self, key, rec):
        """Play a write made through REST/MCP: store a full record under a new rev."""
        wiki = self.wikis[key]
        wiki.records[rec["id"]] = {**rec, "rev": wiki.next_rev(), "deleted": False, "device": "rest"}
        m = re.fullmatch(r"([A-Z])(\d+)", rec["id"])
        if m:
            wiki.bump(m.group(1), int(m.group(2)) + 1)

    def wiki(self, key):
        return self.wikis[key]

    def live(self, key):
        return {i: r for i, r in self.wikis[key].records.items() if not r["deleted"]}
