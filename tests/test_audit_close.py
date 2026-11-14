"""The audit at the feature's close: a child process never imports a look-alike package from its
cwd, and closing a parent never closes the operator's task under it. Neutral fixtures only."""

import subprocess
from datetime import datetime, timedelta, timezone

from shapa import config, db, registry, tasks


def test_a_child_process_never_imports_a_shapa_directory_from_its_cwd(tmp_path):
    # A hook runs with the session's repo as its cwd; that repo may hold its own `shapa/` package.
    fake = tmp_path / "repo" / "shapa"
    fake.mkdir(parents=True)
    marker = tmp_path / "ran"
    (fake / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (fake / "__main__.py").write_text("")
    argv = config.self_argv("shapa")
    assert "-P" in argv
    done = subprocess.run([*argv, "ledger", "--help"], cwd=fake.parent, env=config.self_env(),
                          capture_output=True, timeout=60)
    assert not marker.exists()  # the repo's package was not imported
    assert done.returncode == 0 and b"usage" in done.stdout.lower()  # this package ran instead


def test_closing_a_parent_leaves_the_operators_task_open_and_standing_alone(wikis):
    registry.write_format(wikis.wiki)
    conn = wikis.conn()
    try:
        feature = db.add_item(conn, "F", "Shipped feature")
        job = db.add_item(conn, "J", "A job", parent=feature)
        errand, _ = tasks.add_task(conn, "Operator errand under a job", "human", parent=job)
        plain = db.add_item(conn, "T", "Plain task under the job", parent=job)
        closed = db.close(conn, feature, "merge")
        assert errand not in closed and plain in closed
        kept = db.get_item(conn, errand)
        assert kept.status == "open" and kept.parent is None
        db.sweep(conn, now=datetime.now(timezone.utc) + timedelta(days=1))
        assert db.get_item(conn, errand).status == "open"  # the next sweep does not delete it
    finally:
        conn.close()
