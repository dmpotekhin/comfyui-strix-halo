"""Two §14 promises that are easy to claim and easy to get wrong: SSRF and robots.txt.

Both live in ``net`` because every provider fetches through it, so a test here protects all of
them at once.
"""
from __future__ import annotations

import pytest

from app.discovery import net


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8188/prompt",
    "http://localhost:8000/api/users",
    "http://192.168.31.60:8000/api/health",
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]:8128/",
    "http://0.0.0.0:8188/",
])
def test_addresses_inside_the_machine_are_refused(url):
    with pytest.raises(net.BlockedAddress):
        net.check_public_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_only_http_and_https_are_followed(url):
    with pytest.raises(net.BlockedAddress):
        net.check_public_url(url)


def test_a_public_hostname_is_allowed(monkeypatch):
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("140.82.121.4", 443))])
    net.check_public_url("https://github.com/cubiq/ComfyUI_Workflows")


def test_a_hostname_that_resolves_into_the_lan_is_refused(monkeypatch):
    """localtest.me and friends resolve to loopback; a name must not buy an exception."""
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(net.BlockedAddress):
        net.check_public_url("http://localtest.me:8000/api/users")


def test_a_hostname_with_a_private_answer_among_public_ones_is_refused(monkeypatch):
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("140.82.121.4", 443)),
                                         (2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(net.BlockedAddress):
        net.check_public_url("https://mixed.example.com/")


def test_the_real_transport_checks_before_it_connects(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda *a, **k: called.append("opened"))
    with pytest.raises(net.BlockedAddress):
        net._urllib_transport("http://127.0.0.1:8188/prompt", 5.0, {})
    assert called == [], "the socket must never be opened for a blocked address"


# --- robots.txt ------------------------------------------------------------------------------

def test_rules_for_our_agent_win_over_the_wildcard():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: comfyui-studio\nDisallow: /private\n"
    assert net._robots_rules(text) == [("/private", False)]


def test_the_wildcard_is_used_when_we_are_not_named():
    text = "User-agent: *\nDisallow: /admin\nAllow: /admin/public\n"
    assert net._robots_rules(text) == [("/admin", False), ("/admin/public", True)]


def test_robots_blocks_a_disallowed_path(monkeypatch):
    net._robots_cache.clear()
    monkeypatch.setattr(net, "get",
                        lambda url, **k: net.HttpResponse(url=url, status=200, body=
                                                          b"User-agent: *\nDisallow: /workflows\n"))
    assert net.robots_allows("https://example.com/workflows/page-1") is False
    assert net.robots_allows("https://example.com/about") is True


def test_a_missing_robots_file_is_permission(monkeypatch):
    net._robots_cache.clear()

    def boom(url, **kwargs):
        raise net.FetchError("HTTP 404 for robots")

    monkeypatch.setattr(net, "get", boom)
    assert net.robots_allows("https://example.com/anything") is True


def test_the_longest_matching_rule_decides(monkeypatch):
    net._robots_cache.clear()
    monkeypatch.setattr(net, "get",
                        lambda url, **k: net.HttpResponse(url=url, status=200, body=
                                                          b"User-agent: *\nDisallow: /w\n"
                                                          b"Allow: /w/open\n"))
    assert net.robots_allows("https://example.com/w/secret") is False
    assert net.robots_allows("https://example.com/w/open/list") is True


def test_get_enforces_robots_only_when_asked(monkeypatch):
    net._robots_cache.clear()
    monkeypatch.setattr(net, "get", net.get)          # keep the real implementation
    monkeypatch.setattr(net, "_transport",
                        lambda url, timeout, headers: net.HttpResponse(
                            url=url, status=200, body=b"<html>ok</html>"))
    monkeypatch.setattr(net, "robots_allows", lambda url, **k: False)
    with pytest.raises(net.BlockedAddress):
        net.get("https://example.com/page", robots=True)
    assert net.get("https://example.com/page").status == 200
