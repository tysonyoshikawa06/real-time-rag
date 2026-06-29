"""Embedding interface and local sentence-transformer implementation.

The Embedder ABC is the only coupling point between the consumer write path
and any specific model. To swap in Voyage AI or OpenAI embeddings later,
add a new subclass and pass it to write_batch — nothing else changes.
"""

from abc import ABC, abstractmethod

import numpy as np
from sentence_transformers import SentenceTransformer


class Embedder(ABC):
    """Converts a list of strings into a list of float vectors."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one normalized embedding vector per input text.

        Vectors must be L2-normalized so that dot product equals cosine
        similarity. This is what pgvector's <=> operator (cosine distance)
        and the HNSW index (Step 10) both expect.
        """


class LocalEmbedder(Embedder):
    """Wraps all-MiniLM-L6-v2 via sentence-transformers.

    Produces 384-dimensional, cosine-normalized vectors. The model file is
    ~80 MB and downloads to the HuggingFace cache (~/.cache/huggingface/) on
    first use; subsequent runs load from disk and are fast.

    The model is loaded once on construction and reused across all embed()
    calls — never reloaded per batch.
    """

    _EXPECTED_DIM = 384

    def __init__(self) -> None:
        self._model = SentenceTransformer("all-MiniLM-L6-v2")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs: np.ndarray = self._model.encode(texts, normalize_embeddings=True)
        assert vecs.shape[1] == self._EXPECTED_DIM, (
            f"Model returned {vecs.shape[1]}-dim vectors, expected {self._EXPECTED_DIM}. "
            "Wrong model loaded?"
        )
        return vecs.tolist()
