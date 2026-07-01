"""Optional local embeddings.

Uses ``sentence-transformers`` (model all-MiniLM-L6-v2) when it is installed,
giving true semantic similarity. When it is not installed, ``available()``
returns False and callers fall back to the lexical methods (BM25 in fetch,
token-Jaccard in maintain). Install to activate::

    pip install sentence-transformers      # see requirements.txt

Embeddings are cached per note in a sidecar file inside the (external) wiki
directory, so a note is only re-embedded when its content changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_MODEL = None
_AVAILABLE: bool | None = None
_MODEL_NAME = "all-MiniLM-L6-v2"
_CACHE_FILE = ".shapa-vectors.json"


def available() -> bool:
    """True if a local embedding model could be loaded."""
    global _AVAILABLE, _MODEL
    if _AVAILABLE is None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            _MODEL = SentenceTransformer(_MODEL_NAME)
            _AVAILABLE = True
        except Exception:
            _AVAILABLE = False
    return _AVAILABLE


def embed_one(text: str) -> list[float]:
    """Embed a single string (normalized). Requires available()."""
    return _MODEL.encode([text], normalize_embeddings=True)[0].tolist()  # type: ignore


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two normalized vectors."""
    import numpy as np
    if not a or not b:
        return 0.0
    return float(np.dot(np.asarray(a), np.asarray(b)))


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def note_vectors(directory, texts: dict[str, str]) -> dict[str, list[float]]:
    """Return {id: vector} for *texts*, caching by content hash in the dir.

    Only callable when available(); embeds just the notes whose content changed.
    """
    directory = Path(directory)
    cache_path = directory / _CACHE_FILE
    cache = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            cache = {}

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
    if dirty:
        try:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return out
