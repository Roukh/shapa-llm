"""Break-it (review): an un-share done on the server (stage:0 taken off a note by REST) is undone by the next
local edit of that note, because the push carries the stale tags kept in the sync state."""

from tests.test_sync import KEY, Device, HostedDouble, note_rec  # noqa: F401
from tests.test_sync import a, double  # noqa: F401


def test_a_local_edit_after_a_rest_unshare_puts_the_marker_back(a, double):
    a.sync()
    double.remote_put(KEY, note_rec("research/shared.md", "v1\n", tags=["stage:0"], updated="2026-01-01T00:00:00Z"))
    a.sync()
    assert (a.wiki / "research" / "shared.md").read_text() == "v1\n"
    # the operator hides the note again from the outside agents (REST PATCH tags: []), body unchanged
    double.remote_put(KEY, note_rec("research/shared.md", "v1\n", tags=[], updated="2026-01-02T00:00:00Z"))
    # before the laptop syncs, the operator edits the file (its mtime is later than the REST edit)
    (a.wiki / "research" / "shared.md").write_text("v2 local\n", encoding="utf-8")
    a.sync()
    a.sync()
    live = double.live(KEY)["research/shared.md"]
    print("server now:", live["data"]["body"], live["tags"])
    assert "stage:0" not in live["tags"], "the stale marker was pushed back: the note is visible to baseline devices again"
