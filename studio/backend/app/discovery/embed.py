"""Embedding backends for local, multilingual search.

The rule from the brief — do not download an embedding model without asking — is implemented as a
resolution step rather than a comment. :func:`resolve` looks for a model *already on disk* under the
configured directory. If it is there, ``sentence-transformers`` is imported and used. If it is not,
search still works: it falls back to a deterministic hashed n-gram backend, which is honest about what
it is (lexical similarity that happens to bridge some morphology, not semantics) and reports itself as
such in every search response.

Nothing in this module reaches the network.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_MODEL_DIR = "~/.cache/comfyui-studio/embeddings"
HASH_DIM = 512

_WORD = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def tokens(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


class EmbeddingBackend:
    """Minimal contract: text in, fixed-width vectors out."""

    name = "none"
    dim = 0
    semantic = False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"model": self.name, "dim": self.dim, "semantic": self.semantic}


class HashingBackend(EmbeddingBackend):
    """Deterministic character+word hashing. Offline, no model, no download — and not semantic."""

    name = "hash-ngram"
    dim = HASH_DIM
    semantic = False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            words = tokens(text)
            features = list(words)
            for word in words:
                padded = f"#{word}#"
                for size in (3, 4, 5):
                    for index in range(0, max(1, len(padded) - size + 1)):
                        features.append(padded[index:index + size])
            for feature in features:
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[bucket] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            out.append([value / norm for value in vector])
        return out


class SentenceTransformerBackend(EmbeddingBackend):
    """``sentence-transformers`` over a model directory that already exists on disk."""

    semantic = True

    def __init__(self, model: Any, name: str, dim: int) -> None:
        self._model = model
        self.name = name
        self.dim = dim

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True,
                                     show_progress_bar=False)
        return [list(map(float, vector)) for vector in vectors]


@dataclass
class Resolution:
    backend: EmbeddingBackend
    note: str = ""
    requested: str = ""
    available_models: list[str] = field(default_factory=list)

    def info(self) -> dict[str, Any]:
        return {**self.backend.info(), "note": self.note, "requested": self.requested,
                "available_models": self.available_models}


def _model_dir(base: str | os.PathLike[str] | None) -> Path:
    return Path(base or DEFAULT_MODEL_DIR).expanduser()


def available_models(base: str | os.PathLike[str] | None = None) -> list[str]:
    root = _model_dir(base)
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        # A sentence-transformers directory always ships these.
        if (entry / "config.json").is_file() and (
                (entry / "modules.json").is_file() or (entry / "model.safetensors").is_file()
                or (entry / "pytorch_model.bin").is_file()):
            out.append(entry.name)
    return out


def resolve(*, model_name: str = DEFAULT_MODEL_NAME, model_dir: str | None = None,
            prefer_semantic: bool = True) -> Resolution:
    """Pick the best backend that is *already available*, and say which one and why."""
    root = _model_dir(model_dir)
    models = available_models(root)
    if prefer_semantic:
        candidate = root / model_name
        if candidate.is_dir() and (candidate / "config.json").is_file():
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except Exception as exc:            # not installed, broken install, missing torch
                return Resolution(HashingBackend(),
                                  note=(f"the multilingual model is on disk but sentence-transformers "
                                        f"could not be loaded ({type(exc).__name__}); using the "
                                        f"offline hashing backend"),
                                  requested=model_name, available_models=models)
            try:
                loaded = SentenceTransformer(str(candidate), device="cpu")
                dim = int(loaded.get_sentence_embedding_dimension() or 0)
                return Resolution(SentenceTransformerBackend(loaded, model_name, dim),
                                  note=f"semantic model loaded from {candidate}",
                                  requested=model_name, available_models=models)
            except Exception as exc:
                return Resolution(HashingBackend(),
                                  note=(f"the multilingual model could not be initialised "
                                        f"({type(exc).__name__}: {exc}); using the offline hashing "
                                        f"backend"),
                                  requested=model_name, available_models=models)
        wanted_size = "about 470 MB"
        place = f"{candidate}"
        return Resolution(
            HashingBackend(),
            note=(f"no local copy of {model_name} ({wanted_size}) at {place}. Semantic search is "
                  f"available after the model is placed there; until then ranking uses keyword "
                  f"matching plus an offline hashing backend, which is lexical, not semantic"),
            requested=model_name, available_models=models)
    return Resolution(HashingBackend(), note="semantic backend disabled by configuration",
                      requested=model_name, available_models=models)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def pack(vector: Sequence[float]) -> bytes:
    """Store vectors compactly: float32 little-endian."""
    import array
    return array.array("f", [float(v) for v in vector]).tobytes()


def unpack(blob: bytes) -> list[float]:
    import array
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def embedding_text(row: dict[str, Any]) -> str:
    """The text a resource is indexed by — the same fields a person would read."""
    graph = row.get("workflow_json") or {}
    classes: list[str] = []
    if isinstance(graph, dict):
        nodes = graph.get("nodes")
        if isinstance(nodes, list):
            classes = [str(n.get("type")) for n in nodes if isinstance(n, dict) and n.get("type")]
        elif graph:
            classes = [str(n.get("class_type")) for n in graph.values()
                       if isinstance(n, dict) and n.get("class_type")]
    metadata = row.get("metadata") or {}
    loras = metadata.get("loras") or []
    parts: Iterable[str] = [
        str(row.get("title") or ""),
        str(row.get("description") or "")[:1500],
        " ".join(str(t) for t in (row.get("tags") or [])),
        str(row.get("category") or ""),
        " ".join(classes),
        " ".join(str(x) for x in loras),
    ]
    return "\n".join(part for part in parts if part)
