"""``shapa remote``: login, credentials, status, sync and the session hook."""

import io
import json
import os
import stat
import sys
import threading
import time

import pytest

from shapa import cli, db, remote, remotehttp, remotesync, syncstate
from tests.hosted_double import OWNER_KEY, HostedDouble

URL = "https://hosted.example"


@pytest.fixture
def double(monkeypatch):
    d = HostedDouble()
    monkeypatch.setattr(remote, "TRANSPORT", d.transport)
    return d


@pytest.fixture
def logged_in(double, wikis):
    did, token = double.add_device("laptop")
    syncstate.save_credentials(syncstate.Credentials(URL, "laptop", did, token))
    return token


def run(capsys, argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = remote.run(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# --- URLs ---------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://hosted.example", "https://hosted.example/base/",
                                 "http://127.0.0.1:8787", "http://localhost"])
def test_good_urls(url):
    assert remotehttp.check_url(url).startswith("http")
    assert not remotehttp.check_url(url).endswith("/")


@pytest.mark.parametrize("url", ["http://hosted.example", "http://127.0.0.1.evil.example",
                                 "ftp://hosted.example", "https://user:pw@hosted.example",
                                 "https://hosted.example?x=1", "https://hosted.example#frag",
                                 "", "hosted.example", "https://", "http://localhost:notaport"])
def test_bad_urls(url):
    with pytest.raises(remotehttp.RemoteError):
        remotehttp.check_url(url)


# --- login ------------------------------------------------------------------------------------

def test_login_with_the_owner_key_from_the_environment(double, capsys, monkeypatch):
    monkeypatch.setenv("SHAPA_OWNER_KEY", OWNER_KEY)
    code, out, err = run(capsys, ["login", URL, "--device", "my laptop"])
    assert code == 0 and "logged in as my laptop" in out
    rdir = syncstate.remote_dir()
    cfg = json.loads((rdir / "remote.json").read_text())
    assert set(cfg) == {"url", "device", "device_id"} and cfg["url"] == URL
    token = (rdir / "remote.token").read_text().strip()
    assert stat.S_IMODE((rdir / "remote.token").stat().st_mode) == 0o600
    for blob in (out, err, json.dumps(cfg)):
        assert token not in blob and OWNER_KEY not in blob
    for f in rdir.rglob("*"):
        if f.is_file():
            assert OWNER_KEY not in f.read_text()
    creds = syncstate.load_credentials()
    assert creds.token == token and token not in repr(creds)
    assert double.devices["d1"]["scopes"] == ["delete", "read", "write"]


def test_login_prompts_for_the_owner_key_when_the_environment_has_none(double, capsys, monkeypatch):
    monkeypatch.setattr(remote.getpass, "getpass", lambda prompt="": OWNER_KEY)
    code, out, _ = run(capsys, ["login", URL])
    assert code == 0 and OWNER_KEY not in out


def test_there_is_no_way_to_pass_the_owner_key_on_the_command_line(double):
    with pytest.raises(SystemExit):
        remote.run(["login", URL, "--owner-key", OWNER_KEY])


def test_login_with_a_wrong_owner_key_fails_without_leaking_it(double, capsys, monkeypatch):
    bad = "wrong-" + "k" * 40
    monkeypatch.setenv("SHAPA_OWNER_KEY", bad)
    code, out, err = run(capsys, ["login", URL])
    assert code == 1 and bad not in out + err
    assert syncstate.load_credentials() is None


def test_login_refuses_plain_http_to_a_remote_host(double, capsys, monkeypatch):
    monkeypatch.setenv("SHAPA_OWNER_KEY", OWNER_KEY)
    code, _, err = run(capsys, ["login", "http://hosted.example"])
    assert code == 1 and "https" in err and double.calls == []


def test_login_with_a_pre_minted_token_on_stdin(double, capsys, monkeypatch):
    did, token = double.add_device("phone")
    code, out, err = run(capsys, ["login", URL, "--token-stdin"], stdin=token + "\n",
                         monkeypatch=monkeypatch)
    assert code == 0 and token not in out + err
    assert syncstate.load_credentials().device == "phone"


def test_login_rejects_a_token_that_is_not_a_token(double, capsys, monkeypatch):
    code, _, err = run(capsys, ["login", URL, "--token-stdin"], stdin="hunter2\n", monkeypatch=monkeypatch)
    assert code == 1 and "hunter2" not in err


def test_a_missing_token_file_means_not_logged_in(logged_in, capsys):
    (syncstate.remote_dir() / "remote.token").unlink()
    assert syncstate.load_credentials() is None
    code, out, _ = run(capsys, ["status", "--check"])
    assert code == 1 and "not logged in" in out


def test_logout_forgets_the_login(logged_in, capsys):
    code, out, _ = run(capsys, ["logout"])
    assert code == 0 and syncstate.load_credentials() is None
    assert not (syncstate.remote_dir() / "remote.token").exists()


def test_the_token_never_appears_in_an_error_message():
    token = "shp_d1_" + "s3cr3t" * 6

    def leaky(method, url, headers, body, timeout):
        msg = json.dumps({"error": {"code": "boom", "message": f"bad bearer {token} \x1b[31m!"}})
        return 500, {}, msg.encode()

    client = remotehttp.Client(URL, token, transport=leaky)
    with pytest.raises(remotehttp.RemoteError) as exc:
        client.request("GET", "/v1/whoami")
    assert token not in str(exc.value) and "\x1b" not in str(exc.value)
    assert token not in repr(client)


# --- status -----------------------------------------------------------------------------------------

def test_status_when_logged_out(capsys):
    assert run(capsys, ["status"])[0] == 0
    code, out, _ = run(capsys, ["status", "--check", "--json"])
    assert code == 1 and json.loads(out)["logged_in"] is False


def test_status_reports_pending_changes_and_never_the_token(logged_in, wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "M", "pending one")
    c.close()
    code, out, _ = run(capsys, ["status", "--cwd", str(wikis.repo), "--json"])
    data = json.loads(out)
    assert code == 0 and logged_in not in out
    assert {w["key"]: w["pending"] for w in data["wikis"]} == {"my-repo": 1, "global": 0}


def test_status_check_exits_1_when_the_server_is_unreachable(logged_in, wikis, capsys, monkeypatch):
    def down(*a):
        raise remotehttp.RemoteError("server unreachable (URLError)")

    monkeypatch.setattr(remote, "TRANSPORT", down)
    code, out, _ = run(capsys, ["status", "--check", "--cwd", str(wikis.repo)])
    assert code == 1 and "unreachable" in out


def test_status_check_is_ok_against_a_live_server(logged_in, wikis, capsys):
    assert run(capsys, ["status", "--check", "--cwd", str(wikis.repo)])[0] == 0


def test_status_check_exits_1_for_a_revoked_device(logged_in, double, wikis, capsys):
    double.devices["d1"]["revoked"] = True
    assert run(capsys, ["status", "--check", "--cwd", str(wikis.repo)])[0] == 1


# --- sync ---------------------------------------------------------------------------------------------

def test_sync_pushes_the_repo_and_global_wikis(logged_in, double, wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "R", "repo rule")
    c.close()
    c = wikis.conn(wikis.global_wiki)
    db.add_row(c, "M", "global memory")
    c.close()
    code, out, err = run(capsys, ["sync", "--cwd", str(wikis.repo)])
    assert code == 0 and logged_in not in out + err
    assert set(double.wikis) == {"my-repo", "global"}
    assert double.wikis["global"].kind == "global" and double.wikis["my-repo"].kind == "repo"
    assert list(double.live("my-repo")) == ["R1"] and list(double.live("global")) == ["M1"]


def test_sync_key_overrides_the_repo_wikis_key(logged_in, double, wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "R", "rule")
    c.close()
    run(capsys, ["sync", "--cwd", str(wikis.repo), "--key", "custom-key"])
    assert "custom-key" in double.wikis and "global" in double.wikis


def test_sync_json_output_has_no_secrets(logged_in, wikis, capsys):
    code, out, _ = run(capsys, ["sync", "--cwd", str(wikis.repo), "--json"])
    assert code == 0 and logged_in not in out
    assert [w["key"] for w in json.loads(out)["wikis"]] == ["my-repo", "global"]


def test_sync_dry_run_prints_the_plan_and_changes_nothing(logged_in, double, wikis, capsys):
    c = wikis.conn()
    db.add_row(c, "R", "rule")
    c.close()
    before = db.db_path(wikis.wiki).read_bytes()
    code, out, _ = run(capsys, ["sync", "--cwd", str(wikis.repo), "--dry-run"])
    assert code == 0 and "would send 1" in out
    assert double.wikis == {} and db.db_path(wikis.wiki).read_bytes() == before


def test_sync_when_not_logged_in_says_so(wikis, capsys):
    code, _, err = run(capsys, ["sync", "--cwd", str(wikis.repo)])
    assert code == 1 and "not logged in" in err


def test_sync_all_reaches_registered_wikis(logged_in, double, wikis, capsys, tmp_path):
    from shapa import registry

    other = tmp_path / "other-repo"
    (other / ".git").mkdir(parents=True)
    (other / ".git" / "config").write_text('[remote "origin"]\n\turl = https://git.example/o/other-repo.git\n')
    (other / ".shapa").mkdir()
    (other / ".shapa" / "AGENTS.md").write_text("m\n", encoding="utf-8")
    c = db.connect(other / ".shapa", create=True)
    db.add_row(c, "M", "elsewhere")
    c.close()
    registry.register([other / ".shapa"], via="test")
    run(capsys, ["sync", "--all", "--cwd", str(wikis.repo)])
    assert "other-repo" in double.wikis


def test_devices_and_instructions(logged_in, double, capsys, monkeypatch):
    monkeypatch.setenv("SHAPA_OWNER_KEY", OWNER_KEY)
    code, out, _ = run(capsys, ["devices"])
    assert code == 0 and "laptop" in out and logged_in not in out
    assert run(capsys, ["devices", "--revoke", "d1"])[0] == 0
    assert double.devices["d1"]["revoked"] is True
    assert run(capsys, ["devices", "--revoke", "../etc"])[0] == 1
    assert run(capsys, ["instructions", "--client", "rest"])[0] == 1  # that device is revoked now
    assert run(capsys, ["instructions", "--client", "x y"])[0] == 1


def test_instructions_print_the_servers_text(logged_in, capsys):
    code, out, _ = run(capsys, ["instructions", "--client", "mcp"])
    assert code == 0 and out.strip() == "Session defaults for mcp"


def test_the_remote_command_is_reachable_from_the_shapa_cli(logged_in, wikis, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["remote", "status", "--cwd", str(wikis.repo)])
    assert exc.value.code == 0 and "logged in as laptop" in capsys.readouterr().out


# --- the hook -------------------------------------------------------------------------------------------

def hook(capsys, monkeypatch, cwd, extra=()):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(cwd), "session_id": "s"})))
    code = remote.run(["sync", "--hook", *extra])
    return code, capsys.readouterr()


def test_hook_not_logged_in_is_a_silent_no_op(double, wikis, capsys, monkeypatch):
    code, cap = hook(capsys, monkeypatch, wikis.repo)
    assert code == 0 and cap.out == "" and cap.err == "" and double.calls == []


def test_hook_syncs_and_prints_at_most_one_line(logged_in, double, wikis, capsys, monkeypatch):
    c = wikis.conn()
    db.add_row(c, "R", "from the hook")
    c.close()
    code, cap = hook(capsys, monkeypatch, wikis.repo)
    assert code == 0 and len(cap.out.strip().splitlines()) == 1 and "sent 1" in cap.out
    assert logged_in not in cap.out
    assert "R1" in double.live("my-repo")
    code, cap = hook(capsys, monkeypatch, wikis.repo)
    assert code == 0 and cap.out == ""


def test_hook_if_changed_skips_the_network_when_nothing_changed(logged_in, double, wikis, capsys,
                                                                monkeypatch):
    hook(capsys, monkeypatch, wikis.repo)
    calls = len(double.calls)
    hook(capsys, monkeypatch, wikis.repo, ["--if-changed"])
    assert len(double.calls) == calls


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"cwd": 5}', '{"cwd": "/nonexistent/x"}'])
def test_hook_survives_bad_stdin(logged_in, wikis, capsys, monkeypatch, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert remote.run(["sync", "--hook"]) == 0


def test_hook_swallows_every_error_and_exits_zero(logged_in, wikis, capsys, monkeypatch):
    def boom(*a):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(remote, "TRANSPORT", boom)
    code, cap = hook(capsys, monkeypatch, wikis.repo)
    assert code == 0 and cap.out == "" and cap.err == ""
    monkeypatch.setattr(remotesync, "run_sync", boom)
    assert hook(capsys, monkeypatch, wikis.repo)[0] == 0


def test_hook_swallows_a_bad_flag_too(logged_in, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert remote.run(["sync", "--hook", "--no-such-flag"]) == 0


def test_hook_stays_within_its_budget_when_the_server_hangs(logged_in, double, wikis, capsys,
                                                            monkeypatch):
    monkeypatch.setattr(remotesync, "HOOK_BUDGET", 0.5)
    c = wikis.conn()
    db.add_row(c, "M", "hangs")
    c.close()
    release = threading.Event()
    double.hang = release
    start = time.monotonic()
    code, cap = hook(capsys, monkeypatch, wikis.repo)
    elapsed = time.monotonic() - start
    release.set()
    assert code == 0 and cap.out == "" and elapsed < 3
    time.sleep(0.3)  # the abandoned worker wakes up, sees the deadline passed, applies nothing
    assert not list((syncstate.remote_dir() / "sync").glob("*.json"))
    c = wikis.conn()
    try:
        assert [r.id for r in db.rows(c)] == ["M1"] and db.tombstones(c) == []
    finally:
        c.close()


# --- helpers: keys, origins, scope ------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [("My Repo", "my-repo"), ("shapa-llm", "shapa-llm"),
                                           ("Ünï.code", "n-.code"),
                                           ("---", ""), ("проект", ""), (".hidden", "hidden"),
                                           ("UPPER_case", "upper_case"), ("a" * 100, "a" * 64)])
def test_slug(name, expected):
    out = remotesync.slug(name)
    assert out == expected and (out == "" or remotesync.KEY_RE.match(out))


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/owner/repo.git", "github.com/owner/repo"),
    ("git@github.com:owner/repo.git", "github.com/owner/repo"),
    ("https://user:secret@github.com/owner/repo", "github.com/owner/repo"),
    ("ssh://git@host.example:2222/owner/repo.git", "host.example:2222/owner/repo"),
    ("not a url at all", ""),
])
def test_git_origin_drops_credentials(wikis, url, expected):
    (wikis.repo / ".git" / "config").write_text(
        f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/*\n',
        encoding="utf-8")
    got = remotesync.git_origin(wikis.wiki)
    assert got == expected and "secret" not in got


def test_the_global_wiki_key_is_global_and_a_repo_wikis_is_its_slug(wikis):
    assert remotesync.wiki_key(wikis.global_wiki) == "global"
    assert remotesync.wiki_key(wikis.wiki) == "my-repo"
    assert remotesync.wiki_key(wikis.wiki, "forced") == "forced"


# --- the real transport (loopback) -------------------------------------------------------------------------

def _serve(handler_cls):
    import http.server

    try:
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    except OSError:
        pytest.skip("loopback sockets are not available here")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_real_transport_does_not_follow_a_redirect_to_another_host():
    import http.server

    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        log_message = lambda *a: None  # noqa: E731

    target = _serve(Target)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
            self.end_headers()

        log_message = lambda *a: None  # noqa: E731

    redirector = _serve(Redirector)
    client = remotehttp.Client(f"http://127.0.0.1:{redirector.server_port}", "shp_d1_secretsecret")
    try:
        with pytest.raises(remotehttp.RemoteError) as exc:
            client.request("GET", "/v1/whoami", timeout=5)
    finally:
        redirector.shutdown()
        target.shutdown()
    assert "redirect" in str(exc.value) and seen == []


def test_the_real_transport_round_trips_json_over_loopback():
    import http.server

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"auth": self.headers.get("Authorization"), "ua": self.headers.get(
                "User-Agent")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        log_message = lambda *a: None  # noqa: E731

    srv = _serve(Echo)
    try:
        out = remotehttp.Client(f"http://127.0.0.1:{srv.server_port}", "shp_d1_tok").request(
            "GET", "/v1/whoami", timeout=5)
    finally:
        srv.shutdown()
    assert out["auth"] == "Bearer shp_d1_tok" and out["ua"].startswith("shapa/")


def test_an_oversized_response_is_refused():
    def huge(method, url, headers, body, timeout):
        return 200, {}, b"x" * (remotehttp.MAX_RESPONSE_BYTES + 1)

    with pytest.raises(remotehttp.RemoteError):
        remotehttp.Client(URL, "shp_d1_tok", transport=huge).request("GET", "/v1/whoami")


@pytest.mark.parametrize("payload", [b"<html>captive portal</html>", b"[1,2]", b"\xff\xfe", b""])
def test_a_non_json_or_wrong_shape_response_is_refused(payload):
    def odd(method, url, headers, body, timeout):
        return 200, {}, payload

    with pytest.raises(remotehttp.RemoteError):
        remotehttp.Client(URL, "shp_d1_tok", transport=odd).request("GET", "/v1/whoami")


def test_a_bad_key_flag_is_refused_and_never_reaches_a_url(logged_in, double, wikis, capsys):
    code, _, err = run(capsys, ["sync", "--cwd", str(wikis.repo), "--key", "../../admin"])
    assert code == 1 and double.calls == []
    code, _, err = run(capsys, ["sync", "--all", "--key", "x", "--cwd", str(wikis.repo)])
    assert code == 1 and double.calls == []


def test_the_request_deadline_is_enforced_before_any_call():
    calls = []
    client = remotehttp.Client(URL, "shp_d1_tok", transport=lambda *a: calls.append(a) or (200, {}, b"{}"),
                               deadline=time.monotonic() - 1)
    with pytest.raises(remotehttp.RemoteError):
        client.request("GET", "/v1/whoami")
    assert calls == []


def test_sync_output_says_when_notes_and_oversized_records_were_skipped(logged_in, wikis, capsys,
                                                                        monkeypatch):
    from shapa import sync

    monkeypatch.setattr(sync, "notes_enabled", lambda wiki_dir: False)
    c = wikis.conn()
    db.add_row(c, "M", "huge", "q" * 1_200_000)
    c.close()
    code, out, err = run(capsys, ["sync", "--cwd", str(wikis.repo)])
    assert code == 0 and "note files skipped" in err and "over the server limit" in err


# --- J88: a repo with no git origin has no automatic key ----------------------------------------------------

@pytest.fixture
def no_origin(wikis):
    (wikis.repo / ".git" / "config").unlink()
    c = wikis.conn()
    db.add_row(c, "R", "a rule in a repo with no origin")
    c.close()
    return wikis


def test_sync_without_an_origin_asks_for_a_key_and_sends_nothing_for_that_repo(logged_in, double, no_origin, capsys):
    code, out, err = run(capsys, ["sync", "--cwd", str(no_origin.repo)])
    assert code == 1 and "--key KEY" in err
    assert "my-repo" not in double.wikis and "global" in double.wikis  # the global wiki is unaffected


def test_the_hook_skips_a_repo_with_no_origin_silently(logged_in, double, no_origin, capsys, monkeypatch):
    code, cap = hook(capsys, monkeypatch, no_origin.repo)
    assert code == 0 and cap.out == "" and cap.err == ""
    assert "my-repo" not in double.wikis


def test_status_says_a_repo_needs_a_key(logged_in, no_origin, capsys):
    code, out, _ = run(capsys, ["status", "--cwd", str(no_origin.repo)])
    assert code == 0 and "needs --key" in out
    data = json.loads(run(capsys, ["status", "--cwd", str(no_origin.repo), "--json"])[1])
    assert [w for w in data["wikis"] if w.get("needs_key")]


def test_a_key_passed_once_is_kept_for_later_syncs_and_the_hook(logged_in, double, no_origin, capsys, monkeypatch):
    code, _, _ = run(capsys, ["sync", "--cwd", str(no_origin.repo), "--key", "my-chosen-key"])
    assert code == 0 and "my-chosen-key" in double.wikis
    c = no_origin.conn()
    db.add_row(c, "M", "added later, synced by the hook")
    c.close()
    code, cap = hook(capsys, monkeypatch, no_origin.repo)
    assert code == 0 and "sent 1" in cap.out
    assert len(double.live("my-chosen-key")) == 2 and "my-repo" not in double.wikis
    assert run(capsys, ["sync", "--cwd", str(no_origin.repo)])[0] == 0  # no --key needed any more


def test_a_non_ascii_folder_name_needs_a_key_instead_of_becoming_repo(logged_in, double, tmp_path, capsys):
    folder = tmp_path / "проект"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config").write_text('[remote "origin"]\n\turl = https://git.example/o/r.git\n')
    (folder / ".shapa").mkdir()
    (folder / ".shapa" / "AGENTS.md").write_text("m\n", encoding="utf-8")
    db.connect(folder / ".shapa", create=True).close()
    code, _, err = run(capsys, ["sync", "--cwd", str(folder)])
    assert code == 1 and "--key KEY" in err and "repo" not in double.wikis
