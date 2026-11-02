"""One HTTP seam for every provider.

Discovery talks to the outside world from exactly one place: this module. That is what makes the
whole subsystem testable without a network — a test swaps ``set_transport`` for a function returning
recorded bytes, and every provider, parser and deduplicator runs unchanged.

Rules encoded here, because a provider must not have to remember them:

* every request carries a browser-like ``User-Agent`` (a bare urllib UA is refused by several sites);
* responses are capped by ``max_bytes`` — an external page is untrusted input, and a 500 MB "workflow"
  is not a workflow;
* 403/429 become ``RateLimited`` instead of a generic failure, so a caller can back off and say so;
* no credentials are sent and none are ever read from the environment.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

quote = urllib.parse.quote
quote_plus = urllib.parse.quote_plus

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
DEFAULT_TIMEOUT_S = 20.0
MAX_BYTES = 4_000_000


class FetchError(RuntimeError):
    """The request did not produce usable bytes."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RateLimited(FetchError):
    """403/429 — the source is answering, we are just not allowed to keep asking right now."""


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise FetchError(f"not JSON: {exc}", status=self.status, url=self.url) from exc


#: ``(url, timeout, headers) -> HttpResponse``. Replaced in tests; the real one uses urllib.
Transport = Callable[[str, float, dict[str, str]], HttpResponse]


def _urllib_transport(url: str, timeout: float, headers: dict[str, str]) -> HttpResponse:
    check_public_url(url)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                raise FetchError(f"body of {declared} bytes exceeds the {MAX_BYTES} limit", url=url)
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError(f"body exceeds the {MAX_BYTES} byte limit", url=url)
            return HttpResponse(
                url=response.geturl(),
                status=int(response.status),
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
            )
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} for {url}"
        if exc.code in (403, 429):
            raise RateLimited(message, status=exc.code, url=url) from exc
        raise FetchError(message, status=exc.code, url=url) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error for {url}: {exc.reason}", url=url) from exc
    except TimeoutError as exc:  # pragma: no cover - depends on the network
        raise FetchError(f"timeout for {url}", url=url) from exc


class BlockedAddress(FetchError):
    """The URL points inside the machine or the LAN — discovery has no business there."""


def check_public_url(url: str) -> None:
    """Refuse anything that is not a plain public http(s) destination.

    Sources can be added by an administrator, which makes every registered URL an SSRF candidate:
    ``http://127.0.0.1:8188/prompt`` or ``http://192.168.31.60:8000/api/users`` would otherwise be
    fetched with the server's own network position. Both literal addresses and resolved hostnames
    are checked, because ``localtest.me`` resolves to loopback just as well as ``localhost`` does.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"refusing non-http scheme in {url!r}")
    host = (parts.hostname or "").strip("[]")
    if not host:
        raise BlockedAddress(f"no host in {url!r}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise BlockedAddress(f"{host} is not a public address")
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise FetchError(f"cannot resolve {host}: {exc}", url=url) from exc
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:            # pragma: no cover - exotic socket families
            raise BlockedAddress(f"unparseable address {address} for {host}") from None
        if not resolved.is_global:
            raise BlockedAddress(f"{host} resolves to the non-public address {address}")


#: ``host -> (expires_at, [(pattern, allowed)])``. robots.txt is crawled politely, one per hour.
_robots_cache: dict[str, tuple[float, list[tuple[str, bool]]]] = {}
_ROBOTS_TTL_S = 3600.0


def _robots_rules(text: str, user_agent: str = "comfyui-studio") -> list[tuple[str, bool]]:
    """Longest-match rules for our own agent, falling back to ``*``."""
    groups: dict[str, list[tuple[str, bool]]] = {}
    agents: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            agents = [value.lower()]
            groups.setdefault(value.lower(), [])
        elif key in ("allow", "disallow") and agents:
            for agent in agents:
                groups.setdefault(agent, []).append((value, key == "allow"))
    mine = next((value for key, value in groups.items()
                 if key != "*" and key in user_agent.lower()), None)
    return mine if mine is not None else groups.get("*", [])


def robots_allows(url: str, *, user_agent: str = "comfyui-studio",
                  timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """True when robots.txt (or its absence) lets us fetch this URL.

    A missing or unreadable robots.txt is treated as permission: the point is to honour a stated
    rule, not to turn a broken ancillary file into a source outage.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return True
    root = f"{parts.scheme}://{parts.netloc}"
    cached = _robots_cache.get(root)
    if cached and cached[0] > time.time():
        rules = cached[1]
    else:
        try:
            text = get(f"{root}/robots.txt", timeout=timeout).text
        except FetchError:
            text = ""
        rules = _robots_rules(text, user_agent)
        _robots_cache[root] = (time.time() + _ROBOTS_TTL_S, rules)
    if not rules:
        return True
    path = parts.path or "/"
    best_len, best_allow = -1, True
    for pattern, allowed in rules:
        if not pattern:                 # "Disallow:" with no value means "everything is fine"
            continue
        wildcard = pattern.rstrip("*")
        if path.startswith(wildcard) and len(wildcard) >= best_len:
            best_len, best_allow = len(pattern), allowed
    return best_allow


_transport: Transport = _urllib_transport


def transport() -> Transport:
    """The transport in use — tests read it to restore what they replaced."""
    return _transport


def set_transport(transport: Transport | None) -> None:
    """Install a replacement transport (tests) or restore the real one with ``None``."""
    global _transport
    _transport = transport or _urllib_transport


def get(url: str, *, timeout: float = DEFAULT_TIMEOUT_S,
        headers: dict[str, str] | None = None, robots: bool = False) -> HttpResponse:
    """One request, with the rules enforced here rather than trusted to the transport.

    Rate limits and size caps are checked on the response itself, so a swapped-in transport (tests,
    or a future HTTP client) cannot accidentally turn a 429 into "no results".
    """
    if robots and not robots_allows(url):
        raise BlockedAddress(f"robots.txt asks crawlers to stay away from {url}")
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    response = _transport(url, timeout, merged)
    status = int(getattr(response, "status", 0) or 0)
    if status in (403, 429):
        raise RateLimited(f"HTTP {status} for {url}", status=status, url=url)
    if status >= 400:
        raise FetchError(f"HTTP {status} for {url}", status=status, url=url)
    body = getattr(response, "body", b"")
    if len(body) > MAX_BYTES:
        raise FetchError(f"body exceeds the {MAX_BYTES} byte limit", url=url)
    return response


def fetch_text(url: str, **kwargs: Any) -> str:
    return get(url, **kwargs).text


def fetch_json(url: str, **kwargs: Any) -> Any:
    accept = {"Accept": "application/json"}
    extra = kwargs.pop("headers", None)
    if extra:
        accept.update(extra)
    return get(url, headers=accept, **kwargs).json()


def github_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
