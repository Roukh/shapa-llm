"""Sync sends deletes before puts, and retries a stage-0 record the server refused.

A curated import deletes old baseline rows and writes new ones in one go; sent puts-first, the new
stage-0 rows met a budget still full of the rows about to be deleted and were refused for good.
"""

from shapa import sync


def _rec(rid: str, tags=()) -> dict:
    return {"id": rid, "uid": rid, "kind": "R", "created": "", "updated": "", "rev": 0, "deleted": 0,
            "device": "", "data": {}, "tags": list(tags), "links": []}


def test_deletes_are_sent_before_puts():
    plan = sync.Plan(puts=[(_rec("R1"), 0)], dels=[{"op": "del", "id": "R9", "uid": "R9"}])
    ops = [op for chunk, _ in sync._batches(plan) for op in chunk]
    assert [op["op"] for op in ops] == ["del", "put"]


def test_a_refused_stage0_record_is_offered_again_and_others_are_not():
    state = {"refused": {"R1": "h1", "R2": "h2", "R3": "old"}}
    local = {"R1": _rec("R1", ["stage:0", "principle"]), "R2": _rec("R2", ["topic"]), "R3": _rec("R3")}
    hashes = {"R1": "h1", "R2": "h2", "R3": "new"}
    assert sync._still_refused(state, local, hashes) == {"R2": "h2"}
