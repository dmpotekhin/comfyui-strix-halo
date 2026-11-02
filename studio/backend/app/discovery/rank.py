"""Optional local-LLM reranking, off by default and harmless when absent.

Two hard limits, both because a reranker that misbehaves is worse than no reranker:

* **Loopback only.** The endpoint must resolve to 127.0.0.1 / localhost / ::1. Catalogue rows never
  leave the machine — that is the whole point of a local aggregator, and it is enforced here rather
  than promised in a document.
* **Strict validation.** The model may only reorder what it was given, may only use ids it was given,
  and its answer is discarded wholesale if it does not parse. A reranker can raise or lower a score; it
  can never invent a resource, edit a graph, or hide a result from the list — unscored items keep their
  previous order after the reranked block.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
DEFAULT_TIMEOUT_S = 45.0
MAX_CANDIDATES = 15
MAX_DESCRIPTION = 400

SYSTEM_PROMPT = (
    "You rank ComfyUI workflows and LoRAs against a user request. "
    "Return ONLY a JSON array, no prose, no code fences. Each element: "
    '{"resource_id": "<one of the given ids>", "relevance_score": <0..1>, '
    '"reason": "<one short sentence>", "recommendation": "high"|"medium"|"low"}. '
    "Use only ids from the input. Never invent facts about a workflow. "
    "If a resource is a poor match, say so and score it low."
)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in LOOPBACK_HOSTS


def available(config: dict[str, Any] | None) -> tuple[bool, str]:
    config = config or {}
    url = str(config.get("llm_url") or config.get("base_url") or "")
    if not url:
        return False, ("no local model endpoint configured; AI ranking is disabled and hybrid "
                       "ranking is used")
    if not _is_loopback(url):
        return False, f"refusing to send catalogue data to a non-local endpoint ({url})"
    return True, ""


def _candidates_payload(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in results:
        if len(out) >= MAX_CANDIDATES:
            break
        out.append({
            "resource_id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("type"),
            "description": str(item.get("description") or "")[:MAX_DESCRIPTION],
            "tags": (item.get("tags") or [])[:12],
            "categories": item.get("categories") or [],
            "compatibility": (item.get("compatibility") or {}).get("status"),
        })
    return out


def _parse(answer: str, allowed: set[str]) -> dict[str, dict[str, Any]]:
    text = answer.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("["):] if "[" in text else text
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        resource_id = str(entry.get("resource_id") or "")
        if resource_id not in allowed:
            continue
        raw_score = entry.get("relevance_score")
        if raw_score is None:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        recommendation = str(entry.get("recommendation") or "").lower()
        out[resource_id] = {
            "relevance_score": max(0.0, min(1.0, score)),
            "reason": str(entry.get("reason") or "")[:300],
            "recommendation": recommendation if recommendation in ("high", "medium", "low")
            else ("high" if score >= 0.75 else "medium" if score >= 0.45 else "low"),
        }
    return out


def rerank(query: str, results: list[dict[str, Any]],
           config: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return results reordered (and annotated) plus a report of what happened."""
    ok, why = available(config)
    if not ok:
        return results, {"used": False, "reason": why}
    if not results:
        return results, {"used": False, "reason": "nothing to rerank"}

    config = config or {}
    url = str(config.get("llm_url") or config.get("base_url"))
    model = str(config.get("llm_model") or "local")
    candidates = _candidates_payload(results)
    allowed = {str(item["resource_id"]) for item in candidates if item.get("resource_id")}
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"request": query, "candidates": candidates},
                                                   ensure_ascii=False)},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("llm_timeout_s")
                                                          or DEFAULT_TIMEOUT_S)) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        return results, {"used": False, "reason": f"the local model did not answer: {exc}"}

    answer = ""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        answer = str(message.get("content") or "")
    verdicts = _parse(answer, allowed)
    if not verdicts:
        return results, {"used": False,
                         "reason": "the model's answer was not valid JSON for the given ids; "
                                   "keeping hybrid order"}

    merged: list[dict[str, Any]] = []
    for item in results:
        entry = verdicts.get(str(item.get("id")))
        if entry is None:
            merged.append(item)
            continue
        updated = dict(item)
        updated["ai"] = entry
        # The model's opinion is blended with, not substituted for, the hybrid score: a
        # confident-sounding wrong answer cannot erase a strong lexical match.
        updated["relevance"] = round(0.5 * float(item.get("relevance") or 0.0)
                                     + 0.5 * entry["relevance_score"], 4)
        merged.append(updated)
    merged.sort(key=lambda item: -float(item.get("relevance") or 0.0))
    return merged, {"used": True, "reranked": len(verdicts), "candidates": len(candidates),
                    "model": model}
