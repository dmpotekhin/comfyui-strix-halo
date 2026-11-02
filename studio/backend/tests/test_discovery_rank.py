"""The optional reranker: loopback-only, and it may reorder but never invent.

These tests stand up a throwaway HTTP server on 127.0.0.1, because the module's whole safety
argument is that it only ever talks to a machine-local endpoint. If that rule breaks, catalogue
rows leave the machine.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.discovery import rank


def _serving(answer: str, status: int = 200):
    """Start a one-request server on loopback and return its base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:                                  # noqa: N802 - stdlib API
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:                       # keep pytest output clean
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}/v1/chat/completions", server


def _results() -> list[dict]:
    return [
        {"id": "dsc_aaa", "title": "anime portrait", "relevance": 0.9, "type": "workflow"},
        {"id": "dsc_bbb", "title": "anime upscale", "relevance": 0.5, "type": "workflow"},
    ]


def test_no_endpoint_configured_means_hybrid_ranking(monkeypatch):
    used, reason = rank.available({})
    assert used is False
    assert "no local model endpoint" in reason


def test_a_remote_endpoint_is_refused_outright():
    """Catalogue rows must never be shipped to a hosted model, however it is spelled."""
    used, reason = rank.available({"llm_url": "https://api.openai.com/v1/chat/completions"})
    assert used is False
    assert "non-local endpoint" in reason

    for local in ("http://127.0.0.1:1234/v1", "http://localhost:1234/v1", "http://[::1]:1234/v1"):
        assert rank.available({"llm_url": local})[0] is True


def test_rerank_without_a_model_returns_the_input_untouched():
    results = _results()
    out, report = rank.rerank("anime portrait", results, {"llm_url": ""})
    assert out == results
    assert report["used"] is False


def test_rerank_on_an_empty_candidate_list_does_nothing():
    out, report = rank.rerank("anime", [], {"llm_url": "http://127.0.0.1:9/v1"})
    assert out == []
    assert report["reason"] == "nothing to rerank"


def test_a_valid_answer_reorders_and_is_blended_with_the_hybrid_score():
    answer = json.dumps([
        {"resource_id": "dsc_bbb", "relevance_score": 0.95, "reason": "closer match",
         "recommendation": "high"},
        {"resource_id": "dsc_aaa", "relevance_score": 0.10, "reason": "looser match",
         "recommendation": "low"},
    ])
    url, server = _serving(answer)
    try:
        out, report = rank.rerank("anime", _results(), {"llm_url": url, "llm_model": "local-test"})
    finally:
        server.shutdown()
    assert report["used"] is True
    assert report["model"] == "local-test"
    assert [item["id"] for item in out] == ["dsc_bbb", "dsc_aaa"]
    # 0.5 * 0.5 + 0.5 * 0.95 — the model may move a row, not erase the lexical match.
    assert out[0]["relevance"] == pytest.approx(0.725, abs=1e-3)
    assert out[0]["ai"]["recommendation"] == "high"


def test_an_answer_with_made_up_ids_is_discarded():
    answer = json.dumps([{"resource_id": "dsc_invented", "relevance_score": 0.99}])
    url, server = _serving(answer)
    try:
        out, report = rank.rerank("anime", _results(), {"llm_url": url})
    finally:
        server.shutdown()
    assert report["used"] is False
    assert [item["id"] for item in out] == ["dsc_aaa", "dsc_bbb"]


def test_prose_instead_of_json_keeps_the_hybrid_order():
    url, server = _serving("Sure! The best one is the second, I think.")
    try:
        out, report = rank.rerank("anime", _results(), {"llm_url": url})
    finally:
        server.shutdown()
    assert report["used"] is False
    assert "not valid JSON" in report["reason"]
    assert [item["id"] for item in out] == ["dsc_aaa", "dsc_bbb"]


def test_an_unreachable_model_is_reported_not_raised():
    out, report = rank.rerank("anime", _results(),
                              {"llm_url": "http://127.0.0.1:9/v1/chat/completions",
                               "llm_timeout_s": 1})
    assert report["used"] is False
    assert "did not answer" in report["reason"]
    assert out == _results()
