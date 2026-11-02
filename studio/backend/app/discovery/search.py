"""Hybrid search: keywords, tags, graph evidence, and — when a local model exists — vectors.

Three deliberate choices:

* **Honest fusion.** Keyword and vector scores are combined with fixed weights and both are reported
  per result, so a surprising hit can be inspected instead of shrugged at.
* **Bilingual by construction.** A small Russian→English expansion table sits in front of scoring,
  because the catalogue is overwhelmingly English and the queries are not. This is a bridge, not
  translation: it works offline and never calls a service.
* **Local first, external on request.** Searching this machine's rows is instant and private. Reaching
  out to Civitai or Hugging Face happens only when the caller asks, and then the same ranking rules
  apply to what comes back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import embed
from .analyze import detect_categories, node_classes
from .model import graph_of
from .store import DiscoveryStore

#: Russian and mixed-language query terms → the English vocabulary the catalogue uses.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "аниме": ("anime",), "анимешный": ("anime",), "портрет": ("portrait", "face"),
    "лицо": ("face", "portrait"), "лица": ("face", "identity"), "видео": ("video",),
    "изображение": ("image",), "изображения": ("image",), "фото": ("photo", "image"),
    "фотографии": ("photo", "image"), "фотографию": ("photo", "restore"),
    "картинка": ("image",), "картинки": ("image",), "улучшить": ("upscale", "enhance"),
    "увеличить": ("upscale",), "качество": ("quality", "upscale", "enhance"),
    "старой": ("old", "restore"), "старое": ("old", "restore"),
    "реалистичный": ("realistic", "photorealistic"), "реалистичное": ("realistic",),
    "неон": ("neon",), "неоновым": ("neon",), "неоновый": ("neon",), "освещение": ("lighting",),
    "свет": ("lighting",), "стиль": ("style",), "персонаж": ("character",), "персонажи": ("characters",),
    "анимация": ("animation", "animate"), "анимировать": ("animate",),
    "движение": ("motion", "video"), "замена": ("swap", "replace"), "заменой": ("swap",),
    "сохранением": ("preservation", "keep"), "внешности": ("identity", "face"),
    "киберпанк": ("cyberpunk",), "акварель": ("watercolor",), "кинематографичный": ("cinematic",),
    "генерация": ("generation", "generate"), "сгенерировать": ("generate",),
    "улучшение": ("enhance", "restore"), "восстановление": ("restore",),
    "апскейл": ("upscale",), "модель": ("model",), "лора": ("lora",),
    "видеоизображение": ("video", "image"), "аудио": ("audio",), "звук": ("audio", "sound"),
    "речь": ("speech", "tts"), "озвучка": ("tts", "voice"), "инпейнт": ("inpaint",),
    "замена фона": ("background", "inpaint", "edit"), "фон": ("background",),
}

#: Query words that mean "give me a LoRA" rather than "give me a workflow".
LORA_HINTS = ("lora", "лора", "ло́ра", "adapter", "adapters")

_FIELD_WEIGHTS = {"title": 3.0, "tags": 2.0, "classes": 1.5, "description": 1.0,
                  "source": 0.75, "author": 0.75}


_RU_ENDINGS = ("ами", "ями", "ов", "ев", "ей", "ой", "ый", "ий", "ым", "им", "ая", "яя",
               "ое", "ее", "ые", "ие", "ах", "ях", "ом", "ем", "а", "я", "о", "е", "ы", "и",
               "у", "ю")


def stem(word: str) -> str:
    """A deliberately light Russian/English stemmer: enough to match endings, nothing more."""
    word = word.lower()
    if len(word) <= 4:
        return word
    # Strip endings repeatedly: «освещением» → «освещени» → «освещен», which is what «освещение»
    # reduces to as well. Two nouns of the same idea must land on the same key.
    for _ in range(3):
        stripped = False
        for ending in _RU_ENDINGS:
            if len(word) > len(ending) + 3 and word.endswith(ending):
                word = word[: -len(ending)]
                stripped = True
                break
        if not stripped:
            break
    return word[:-1] if len(word) > 6 and word.endswith("s") else word


_SYNONYMS_BY_STEM: dict[str, tuple[str, ...]] = {}


def expand_query(query: str) -> list[str]:
    """Tokens plus English synonyms. Order preserved so the reason strings read naturally."""
    base = embed.tokens(query)
    out: list[str] = list(base)
    for token in base:
        out.extend(SYNONYMS.get(token, ()))
        if not _SYNONYMS_BY_STEM:
            _SYNONYMS_BY_STEM.update({stem(key): value for key, value in SYNONYMS.items()})
        out.extend(_SYNONYMS_BY_STEM.get(stem(token), ()))
    seen: set[str] = set()
    ordered: list[str] = []
    for token in out:
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _resource_fields(row: dict[str, Any]) -> dict[str, str]:
    graph = graph_of(row.get("workflow_json"))
    classes = " ".join(node_classes(graph).keys()) if graph else ""
    return {
        "title": str(row.get("title") or "").lower(),
        "tags": " ".join(str(t) for t in (row.get("tags") or [])).lower(),
        "classes": classes.lower(),
        "description": str(row.get("description") or "").lower()[:4000],
        "source": str(row.get("source_id") or "").lower(),
        "author": str(row.get("author") or "").lower(),
    }


@dataclass
class Filters:
    types: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    compatible_only: bool = False
    local_only: bool = False
    external_only: bool = False
    base_model: str = ""
    min_confidence: float = 0.0

    def wants_lora(self) -> bool:
        return "lora" in [t.lower() for t in self.types]

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> "Filters":
        def listify(value: Any) -> list[str]:
            if not value:
                return []
            if isinstance(value, str):
                return [part.strip() for part in value.split(",") if part.strip()]
            return [str(v) for v in value if str(v).strip()]

        def truthy(value: Any) -> bool:
            return str(value).lower() in ("1", "true", "yes", "on")

        return cls(
            types=listify(params.get("type") or params.get("types")),
            categories=listify(params.get("category") or params.get("categories")),
            source_ids=listify(params.get("source") or params.get("sources")),
            compatible_only=truthy(params.get("compatible_only") or params.get("compatible")),
            local_only=truthy(params.get("local_only")),
            external_only=truthy(params.get("external_only") or params.get("external")),
            base_model=str(params.get("base_model") or ""),
            min_confidence=float(params.get("min_confidence") or 0.0),
        )


def keyword_score(tokens: Sequence[str], row: dict[str, Any], *,
                  originals: int | None = None) -> tuple[float, list[str]]:
    """Field-weighted overlap, normalised to 0..1, with the matched terms as the explanation.

    ``originals`` is how many words the user actually typed. Normalising against that — not against
    the expanded token list — keeps synonym expansion from diluting a good match: a Russian query that
    hits four English ideas should score high, not four-over-eighteen.
    """
    if not tokens:
        return 0.0, []
    fields = _resource_fields(row)
    field_stems = {name: {stem(word) for word in embed.tokens(text)}
                   for name, text in fields.items()}
    score = 0.0
    reasons: list[str] = []
    matched: set[str] = set()
    for token in tokens:
        needle = stem(token)
        for name, weight in _FIELD_WEIGHTS.items():
            haystack = fields.get(name) or ""
            if not haystack:
                continue
            if needle not in field_stems.get(name, set()) and token not in haystack:
                continue
            score += weight
            if name == "title" and len(reasons) < 3:
                reasons.append(f"title mentions “{token}”")
            elif name == "tags" and len(reasons) < 3:
                reasons.append(f"tag “{token}”")
            elif name == "classes" and len(reasons) < 3:
                reasons.append(f"graph uses {token}")
            matched.add(token)
            break
    if not matched:
        return 0.0, []
    typed = max(2, int(originals if originals is not None else len(tokens)))
    normalised = min(1.0, score / (_FIELD_WEIGHTS["title"] * typed))
    return round(normalised, 4), reasons[:3]


def _category_names(row: dict[str, Any], limit: int = 3) -> list[str]:
    stored = (row.get("metadata") or {}).get("categories")
    if isinstance(stored, list) and stored:
        return [str(c.get("name") if isinstance(c, dict) else c) for c in stored][:limit]
    graph = graph_of(row.get("workflow_json"))
    classes = node_classes(graph) if graph else {}
    text = " ".join([str(row.get("title") or ""), str(row.get("description") or ""),
                     " ".join(str(t) for t in (row.get("tags") or []))])
    return [item["name"] for item in detect_categories(classes.keys(), text, limit=limit)]


def passes(row: dict[str, Any], filters: Filters, tokens: Sequence[str]) -> bool:
    type_ = str(row.get("type") or "")
    if filters.types and type_.lower() not in [t.lower() for t in filters.types]:
        return False
    if filters.source_ids and str(row.get("source_id") or "") not in filters.source_ids:
        return False
    if filters.local_only and str(row.get("origin") or "local") != "local":
        return False
    if filters.external_only and str(row.get("origin") or "local") != "external":
        return False
    if filters.compatible_only:
        status = str((row.get("compatibility") or {}).get("status") or "")
        if status not in ("READY", "PARTIAL"):
            return False
    if filters.base_model:
        wanted = filters.base_model.lower()
        base = str((row.get("metadata") or {}).get("base_model") or "").lower()
        if base and wanted not in base:
            return False
        if not base:
            return False
    if filters.categories:
        wanted = {c.lower() for c in filters.categories}
        have = {c.lower() for c in _category_names(row, limit=5)}
        if not (wanted & have):
            return False
    if tokens and filters.wants_lora() and type_ != "lora":
        return False
    return True


class SearchEngine:
    def __init__(self, store: DiscoveryStore, *, backend: embed.EmbeddingBackend | None = None,
                 resolution: embed.Resolution | None = None) -> None:
        self.store = store
        self.resolution = resolution or embed.resolve()
        self.backend = backend or self.resolution.backend

    # -- helpers ------------------------------------------------------------------------
    def _vectors_for(self, query: str) -> tuple[Any, dict[str, list[float]]]:
        if not query.strip():
            return None, {}
        query_vector = self.backend.encode([query])[0]
        stored = {}
        for resource_id, dim, blob in self.store.load_embeddings(self.backend.name):
            if dim == len(query_vector):
                stored[resource_id] = embed.unpack(blob)
        return query_vector, stored

    def _semantic(self, query_vector: Any, stored: dict[str, list[float]],
                  resource_id: str) -> float:
        if query_vector is None:
            return 0.0
        vector = stored.get(resource_id)
        if not vector:
            return 0.0
        return max(0.0, embed.cosine(query_vector, vector))

    # -- entry point --------------------------------------------------------------------
    def search(self, query: str, *, filters: Filters | None = None, limit: int = 20,
               offset: int = 0, extra_rows: Iterable[dict[str, Any]] = (),
               semantic: bool = True) -> dict[str, Any]:
        filters = filters or Filters()
        tokens = expand_query(query)
        rows: list[dict[str, Any]] = []
        for resource in self.store.all_resources():
            row = resource.native_row()
            row["origin"] = "external" if str(row.get("source_id") or "") not in ("local",) else "local"
            rows.append(row)
        for extra in extra_rows:
            row = dict(extra)
            row.setdefault("origin", "local")
            row.setdefault("source_id", "local")
            rows.append(row)

        originals = len(embed.tokens(query))
        candidates = [row for row in rows if passes(row, filters, tokens)]
        query_vector, stored = self._vectors_for(query) if semantic else (None, {})
        scored: list[dict[str, Any]] = []
        for row in candidates:
            keyword, reasons = keyword_score(tokens, row, originals=originals)
            semantic_score = self._semantic(query_vector, stored, str(row.get("id") or ""))
            if semantic and self.backend.semantic:
                score = 0.55 * semantic_score + 0.45 * keyword
            elif semantic:
                # The hashing backend is lexical; blending it as if it were semantic would
                # overstate what it knows, so it only nudges the keyword score.
                score = 0.35 * semantic_score + 0.65 * keyword
            else:
                score = keyword
            if tokens and score <= 0:
                continue
            if score < filters.min_confidence:
                continue
            scored.append({"row": row, "score": round(float(score), 4),
                           "keyword": round(float(keyword), 4),
                           "semantic": round(float(semantic_score), 4),
                           "reasons": reasons})

        scored.sort(key=lambda item: (-item["score"], str(item["row"].get("title") or "")))
        total = len(scored)
        window = scored[max(0, offset): max(0, offset) + max(1, min(int(limit), 50))]
        results = [self._present(item) for item in window]
        notes: list[str] = []
        if self.resolution.note:
            notes.append(self.resolution.note)
        if not tokens:
            notes.append("empty query: results are ordered by recency, filters still apply")
        return {"query": query, "tokens": tokens, "total": total, "limit": limit,
                "offset": offset, "results": results,
                "backend": self.resolution.info(), "notes": notes}

    def _present(self, item: dict[str, Any]) -> dict[str, Any]:
        row = item["row"]
        return {
            "id": row.get("id"),
            "type": row.get("type"),
            "title": row.get("title"),
            "description": row.get("description"),
            "tags": row.get("tags") or [],
            "source_id": row.get("source_id"),
            "source_url": row.get("source_url"),
            "preview_url": row.get("preview_url"),
            "download_url": row.get("download_url"),
            "author": row.get("author"),
            "license": row.get("license"),
            "workflow_format": row.get("workflow_format"),
            "origin": row.get("origin", "local"),
            "compatibility": row.get("compatibility") or {},
            "categories": _category_names(row),
            "stats": row.get("stats") or {},
            "has_graph": graph_of(row.get("workflow_json")) is not None,
            "relevance": item["score"],
            "explain": {
                "keyword": item["keyword"],
                "semantic": item["semantic"],
                "reasons": item["reasons"],
            },
        }
