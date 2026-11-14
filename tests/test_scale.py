"""Evolution test (contract section 7): with ten and a hundred times the data, an agent must
retrieve at the same speed. One global wiki at 500, 5,000 and 50,000 records (rules, memories,
issues, tasks, project documents, skills; about 200 topic tags, 50 projects, links between them),
built by bulk SQL so the build stays inside the time limit, then each read is timed (the median
of repeated runs) and the table is printed (``pytest -s tests/test_scale.py``)."""

import random
import statistics
import time

import pytest

from shapa import bootstrap, config, db, embed, fetch, mcp, memlog, projectfile, registry, relate, tasks
from shapa.config import WikiRoot

SIZES = (500, 5_000, 50_000)
FLOOR_MS = 10.0       # a read under this is fast enough whatever it did at the smallest size
RELATIVE = 3.0        # ... or no slower than this many times its time at the smallest size
SEARCH_GROWTH = 10.0  # search may grow this much for 100x the data
SEARCH_CAP_MS = 150.0
PROJECTS, TOPICS = 50, 200
WORDS = [f"w{i:04d}x" for i in range(10_000)]  # a query word is in about one record in 200
TS = "2026-01-01T00:00:00Z"


def build(root, n: int) -> None:
    """Fill the wiki at *root* with *n* records by bulk SQL, in one transaction."""
    rng = random.Random(7)
    conn = db.connect(root, create=True)
    registry.write_format(root)

    def text(k):
        return " ".join(rng.choice(WORDS) for _ in range(k))

    mri, items, tags, links = [], [], [], []
    with db.write(conn):
        for i in range(1, n + 1):
            r = rng.random()
            tail = [f"topic-{rng.randrange(TOPICS):03d}", f"topic-{rng.randrange(TOPICS):03d}"]
            proj = f"project:p{rng.randrange(PROJECTS):02d}"
            if r < 0.55:
                kind = "M" if r < 0.30 else ("R" if r < 0.45 else "I")
                rid = f"{kind}{i}"
                mri.append((rid, kind, None, f"{text(6)} {i}", text(40), "scale", "", f"h{i}", TS, f"2026-02-{1 + i % 27:02d}T00:00:00Z"))
                tags += [(rid, t) for t in set(tail)] + ([(rid, proj)] if i % 3 == 0 else [])
                if kind == "M" and i % 7 == 0:
                    tags.append((rid, "idea"))
                if i % 5 == 0:
                    links.append((rid, f"R{max(1, i - 3)}", "related"))
            else:
                rid = f"T{i}"
                owner = "human_task" if i % 2 else "agent_task"
                items.append((rid, "T", None, text(5), "", "closed" if i % 11 == 0 else "open", TS,
                              f"2026-03-{1 + i % 27:02d}T00:00:00Z"))
                tags += [(rid, owner), (rid, proj), (rid, tail[0])]
        for k in range(PROJECTS):
            d = f"D{k + 1}"
            conn.execute("INSERT INTO docs(id, alias, title, body, mime, source, hash, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (d, None, f"Project {k:02d}", projectfile.template(),
                                                         "text/markdown", "scale", f"dh{k}", TS, TS))
            conn.execute("INSERT INTO tags(id, tag) VALUES (?, ?)", (d, f"project:p{k:02d}"))
        for j in range(8):  # a baseline that does not grow with the wiki
            rid = f"R{n + 1 + j}"
            mri.append((rid, "R", None, f"Baseline rule {j} " + text(6), text(60), "scale", "", f"b{j}", TS, TS))
            tags.append((rid, "stage:0"))
        conn.executemany("INSERT INTO mri(id, kind, alias, summary, body, source, session, hash, created, "
                         "updated) VALUES (?,?,?,?,?,?,?,?,?,?)", mri)
        conn.executemany("INSERT INTO items(id, kind, parent, title, body, status, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?)", items)
        conn.executemany("INSERT OR IGNORE INTO tags(id, tag) VALUES (?, ?)", tags)
        conn.executemany("INSERT OR IGNORE INTO links(src, dst, rel) VALUES (?,?,?)", links)
    conn.close()


ROUNDS = 15


def probes(root, monkeypatch) -> dict:
    """The reads an agent does, as callables on one wiki."""
    wr = WikiRoot(path=root, kind="global")

    def with_conn(fn):
        def run():
            c = db.connect(root)
            try:
                return fn(c)
            finally:
                c.close()
        return run

    c = db.connect(root)
    try:  # records that exist, carry tags, and (the first) have links: the same low ids at every size
        linked = c.execute("SELECT l.src FROM links l JOIN mri m ON m.id = l.src WHERE m.kind = 'R' "
                           "GROUP BY l.src ORDER BY CAST(substr(l.src, 2) AS INTEGER) LIMIT 1 OFFSET 12").fetchone()[0]
        crowd = c.execute("SELECT m.id FROM mri m WHERE m.kind = 'M' AND NOT EXISTS (SELECT 1 FROM links l "
                          "WHERE l.src = m.id OR l.dst = m.id) ORDER BY CAST(substr(m.id, 2) AS INTEGER) "
                          "LIMIT 1 OFFSET 12").fetchone()[0]
        assert db.get_row(c, linked).tags and len(db.links_of(c, linked)) >= 1 and db.get_row(c, crowd).tags
    finally:
        c.close()

    def get_by_id():
        monkeypatch.setenv(config.ENV_VAR, str(root))  # `get` resolves its wiki from the environment
        out = mcp.tool_get({"id": linked}, None)
        assert "error" not in out
        return out

    def related(rid):
        c = db.connect(root)
        try:
            found = relate.related(c, rid)
        finally:
            c.close()
        assert found  # an answer, never an empty list
        return found

    conn = memlog.open_index(root, embed_vectors=False)  # the derived search index, built once
    conn.close()
    return {"session start": lambda: bootstrap.build_context(roots=[wr]),
            "tag fetch": lambda: fetch.tag_selection(["topic-007"], roots=[wr], k=8),
            "operator tasks": lambda: tasks.open_tasks([root]),
            "tasks by project": lambda: tasks.open_tasks([root], project="p07"),
            "get by id": get_by_id,
            "tags": with_conn(relate.tag_index),
            "project show": with_conn(lambda c: projectfile.show(c, "p07")),
            "project list": with_conn(projectfile.listing),
            "related (linked)": lambda: related(linked),
            "related (shared tags)": lambda: related(crowd),
            "search": lambda: fetch.select_multi(f"{WORDS[3]} {WORDS[150]}", roots=[wr], k=8, read_only=True)}


def measure_all(roots: dict, monkeypatch) -> dict[int, dict[str, float]]:
    """Median ms of each read at each size. The sizes are timed in turn, round after round, so
    a machine that gets busy or quiet part way through slows every size alike."""
    every = {n: probes(root, monkeypatch) for n, root in roots.items()}
    spent = {n: {op: [] for op in every[n]} for n in roots}
    for n in roots:
        for fn in every[n].values():
            fn()  # one unmeasured call: caches and imports are warm, as in a session that has started
    for _ in range(ROUNDS):
        for op in every[SIZES[0]]:
            for n in roots:
                t = time.perf_counter()
                every[n][op]()
                spent[n][op].append((time.perf_counter() - t) * 1000)
    return {n: {op: statistics.median(v) for op, v in ops.items()} for n, ops in spent.items()}


def test_retrieval_does_not_slow_down_as_the_wiki_grows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(embed, "available", lambda: False)  # one lexical mode: the same on every machine
    built, roots = {}, {}
    for n in SIZES:
        root = roots[n] = tmp_path / f"wiki-{n}"
        root.mkdir()
        (root / "AGENTS.md").write_text("# marker\n", encoding="utf-8")
        t = time.perf_counter()
        build(root, n)
        built[n] = time.perf_counter() - t
        assert db.connect(root).execute("SELECT count(*) FROM mri").fetchone()[0] > 0
    table = measure_all(roots, monkeypatch)
    ops = list(table[SIZES[0]])
    with capsys.disabled():
        print("\nmedian ms per read, by records in the wiki")
        print(f"{'':16}" + "".join(f"{n:>12,}" for n in SIZES))
        for op in ops:
            print(f"{op:16}" + "".join(f"{table[n][op]:>12.2f}" for n in SIZES))
        print(f"{'build (s)':16}" + "".join(f"{built[n]:>12.1f}" for n in SIZES))
    small, large = table[SIZES[0]], table[SIZES[-1]]
    for op in ops:
        if op == "search":
            continue
        assert large[op] <= max(RELATIVE * small[op], FLOOR_MS), (
            f"{op}: {small[op]:.2f} ms at {SIZES[0]:,} records, {large[op]:.2f} ms at {SIZES[-1]:,}")
    assert large["search"] < SEARCH_CAP_MS, f"search {large['search']:.1f} ms at {SIZES[-1]:,} records"
    assert large["search"] < SEARCH_GROWTH * small["search"], (
        f"search grew {large['search'] / small['search']:.1f}x for {SIZES[-1] // SIZES[0]}x the data")
