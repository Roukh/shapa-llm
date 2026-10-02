"""Optional local embeddings.

Uses ``model2vec`` (model ``minishlab/potion-base-8M`` - MIT, numpy-only, no
torch, ~94.7% of MiniLM's MTEB at a fraction of the install size/latency;
shapa-backend-spec.md §5) when it is installed, giving true semantic
similarity. When it is not installed, ``available()`` returns False and
callers fall back to the lexical methods (BM25 in fetch, token-Jaccard in
maintain). Install to activate::

    pip install shapa[semantic]      # see pyproject.toml - no torch pulled in

``[embeddings]`` is kept as a deprecated alias for ``[semantic]`` with the
identical dependency, for one release (shapa-backend-spec.md §3).

Embeddings are cached per note in a sidecar file inside the (external) wiki
directory, so a note is only re-embedded when its content changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_MODEL = None
_AVAILABLE: bool | None = None
_MODEL_NAME = "minishlab/potion-base-8M"
#: Public name for the embedding-cache sidecar (mirrors shapa.store's
#: INDEX_FILENAME) - a rebuildable read-path side effect, never
#: authoritative content, so `shapa init` gitignores it too.
CACHE_FILENAME = ".shapa-vectors.json"
_CACHE_FILE = CACHE_FILENAME


def available() -> bool:
    """True if a local embedding model could be loaded."""
    global _AVAILABLE, _MODEL
    if _AVAILABLE is None:
        try:
            from model2vec import StaticModel  # type: ignore
            # normalize=True: vectors come back unit-length, matching the
            # sentence-transformers backend's normalize_embeddings=True this
            # replaces - cosine() below assumes normalized input either way.
            _MODEL = StaticModel.from_pretrained(_MODEL_NAME, normalize=True)
            _AVAILABLE = True
        except Exception:
            _AVAILABLE = False
    return _AVAILABLE


def installed() -> bool:
    """Whether the ``[semantic]`` extra is importable, WITHOUT loading the
    model - for cheap mode reporting on hot paths (session start)."""
    if _AVAILABLE is not None:
        return _AVAILABLE
    import importlib.util

    try:
        return importlib.util.find_spec("model2vec") is not None
    except (ImportError, ValueError):
        return False


def embed_one(text: str) -> list[float]:
    """Embed a single string (normalized). Requires available()."""
    return _MODEL.encode([text])[0].tolist()  # type: ignore


def model_name() -> str:
    """The embedding model's name - the stamp every vector cache carries so
    a backend change never mixes vector spaces."""
    return _MODEL_NAME


def embed_many(texts: list[str]):
    """Embed several strings in one batch (normalized) as a float32 numpy
    array of shape ``(len(texts), dim)``. Requires available() - which also
    guarantees numpy, since model2vec depends on it."""
    import numpy as np

    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    return np.asarray(_MODEL.encode(list(texts)), dtype=np.float32)  # type: ignore


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two normalized vectors.

    numpy is never a core dependency (it only arrives transitively via the
    ``[semantic]`` extra's model2vec) so it is used opportunistically here
    and must degrade to a pure stdlib dot product when it isn't installed -
    this function has to work in a bare-core install, not just when
    ``available()`` is True.
    """
    if not a or not b:
        return 0.0
    try:
        import numpy as np
    except ImportError:
        return float(sum(x * y for x, y in zip(a, b)))
    return float(np.dot(np.asarray(a), np.asarray(b)))


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def note_vectors(directory, texts: dict[str, str],
                  *, read_only: bool = False) -> dict[str, list[float]]:
    """Return {id: vector} for *texts*, caching by content hash in the dir.

    Only callable when available(); embeds just the notes whose content changed.
    The cache is stamped with the embedding model name - switching backends
    (e.g. the sentence-transformers -> model2vec swap, shapa-backend-spec.md
    §5) must never silently mix incompatible vector spaces just because a
    note's content hash happens to still match; a stamp mismatch invalidates
    the whole cache rather than trusting per-note hashes across models.

    ``read_only=True`` still reads an existing cache (freshest vectors it
    has), and still computes vectors in memory for anything missing or
    changed, but never writes ``.shapa-vectors.json`` back - for a caller
    that must leave a wiki byte-for-byte untouched (e.g. ``shapa.mcp``'s
    ``search`` tool; a stray sidecar cache file left behind by a plain
    search was a real incident, shapa-backend-spec.md Slice 6 report).
    """
    directory = Path(directory)
    cache_path = directory / _CACHE_FILE
    cache = {}
    if cache_path.is_file():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            raw = {}
        if isinstance(raw, dict) and raw.get("model") == _MODEL_NAME:
            cache = raw.get("notes", {}) if isinstance(raw.get("notes"), dict) else {}
        # else: cache from a different (or pre-stamp legacy) model - discard.

    out: dict[str, list[float]] = {}
    dirty = False
    for nid, text in texts.items():
        h = _hash(text)
        entry = cache.get(nid)
        if entry and entry.get("h") == h:
            out[nid] = entry["v"]
        else:
            v = embed_one(text)
            out[nid] = v
            cache[nid] = {"h": h, "v": v}
            dirty = True
    # Drop cache entries for notes that no longer exist.
    for gone in set(cache) - set(texts):
        cache.pop(gone, None)
        dirty = True
    if dirty and not read_only:
        try:
            cache_path.write_text(
                json.dumps({"model": _MODEL_NAME, "notes": cache}), encoding="utf-8"
            )
        except OSError:
            pass
    return out
