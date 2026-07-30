"""Embeddings behind a replaceable interface — the LLM pattern again.

The knowledge engine ranks text by vector similarity, and *which* vectors
is a composition decision. The reference backend here is a
**dependency-free hashing embedder**: words and character trigrams are
hashed into a fixed-dimension vector (a classic "hashing trick" /
feature-hashing representation), L2-normalised so cosine similarity is a
dot product. It is deterministic, instant, fully offline — and honest
about what it is: *lexical* similarity with sub-word robustness
(typos/inflections partially overlap through their trigrams), not deep
semantics. "PTO policy" will match "PTO policy for engineers", but not
"vacation rules".

A true semantic backend (sentence-transformers, or an embeddings API) is
a drop-in: subclass :class:`Embedder`, return a normalised vector, keep
the dimension stable — the store records the embedder name and dimension
and refuses to mix incompatible spaces rather than silently comparing
apples to oranges.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(ABC):
    """Turns text into a fixed-dimension, L2-normalised vector."""

    name: str = "abstract"
    dim: int = 0

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a vector of length :attr:`dim` with unit L2 norm
        (or the zero vector for empty/degenerate input)."""


class HashingEmbedder(Embedder):
    """Feature-hashing over words + character trigrams. Zero deps."""

    name = "hashing"

    def __init__(self, dim: int = 256):
        if dim < 16:
            raise ValueError("embedding dimension must be >= 16")
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        tokens = _TOKEN_RE.findall(text.lower())
        features = list(tokens)
        for token in tokens:
            padded = f"^{token}$"
            features.extend(
                padded[index:index + 3]
                for index in range(len(padded) - 2)
            )
        return features

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature in self._features(text):
            digest = hashlib.blake2b(
                feature.encode("utf-8"), digest_size=8
            ).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0.0:
            return vector
        return [component / norm for component in vector]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two same-length vectors (0.0 on mismatch)."""
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class SentenceTransformerEmbedder(Embedder):
    """Real semantic embeddings via ``sentence-transformers`` (optional).

    Lazy heavy import with install guidance — the M5/M10/M11 pattern.
    The store's embedder pin includes the model name, so switching
    models (or from hashing) correctly demands a re-ingest instead of
    silently mixing vector spaces.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ValueError(
                "the semantic embedder requires sentence-transformers: "
                "pip install 'digital-twin-assistant[semantic]' (or set "
                "knowledge.embedder: hashing)"
            ) from exc
        self._model = SentenceTransformer(model)
        self.name = f"sentence_transformers:{model}"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]


def create_embedder(name: str, dim: int) -> Embedder:
    """Build the configured embedder.

    ``hashing`` ships built-in (offline, dependency-free);
    ``semantic`` uses sentence-transformers (optional extra).
    """
    if name == "hashing":
        return HashingEmbedder(dim)
    if name == "semantic":
        return SentenceTransformerEmbedder()
    raise ValueError(
        f"unknown embedder {name!r} — 'hashing' (built-in) or 'semantic' "
        "(pip install 'digital-twin-assistant[semantic]')"
    )
