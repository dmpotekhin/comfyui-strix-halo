"""Discovery-only providers: sites we may read but must not pretend to integrate with.

Three shapes live here because they share one honest answer to "can you give me the workflow?" — no.

* :class:`WebProvider` harvests links from an HTML page. That is what the brief asks for when a site
  has no public API: use it as a source of *discovery*, not as a fake API.
* :class:`YouTubeProvider` reads ``ytInitialData`` out of a search page. Titles, channels and
  descriptions are metadata the page itself ships; videos are never downloaded and no API key is used.
* :class:`RedditProvider` implements the real JSON path, but anonymous Reddit answers 403 from this
  network, so without explicit OAuth credentials it reports ``blocked`` instead of quietly returning
  an empty list that would look like "nothing found".
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from .. import net
from ..classify import title_from_path
from ..model import Resource
from ..sources import SourceSpec
from .base import BaseProvider, ProviderReport, UnsupportedOperation

#: Per-source link patterns. A pattern is a regex whose first group is the path to keep.
LINK_PATTERNS: dict[str, tuple[str, ...]] = {
    "comfy-workflows": (r'href="(/workflows/[a-z0-9][a-z0-9-]{2,80})"',),
    "openart": (r'href="(/workflows/[a-z0-9][a-z0-9-]{2,80})"',
                r'href="(https://openart\.ai/workflows/[a-z0-9][a-z0-9-]{2,80})"'),
    "comfyui-org": (r'href="(https?://comfyui\.org/[a-z]{2}/[a-z0-9][a-z0-9-]{2,80})"',
                    r'href="(/[a-z]{2}/[a-z0-9][a-z0-9-]{2,80})"'),
    "runcomfy": (r'href="(/[a-z0-9][a-z0-9-]{2,60}/?[a-z0-9-]*)"',),
    "comfyvault": (r'href="(/workflows?/[a-z0-9][a-z0-9-]{2,80})"',),
    "comfy-icu": (r'href="(/[a-z0-9][a-z0-9-]{2,60})"',),
    "esheep": (r'href="(/[a-z]+/[a-z0-9]{2,40})"',),
    "aigodlike": (r'href="(/[a-z]+/[a-z0-9][a-z0-9-]{2,60})"',),
}

_MAX_LINKS = 60


class WebProvider(BaseProvider):
    """Harvest outbound workflow links from a public page. Never claims to have the graph."""

    provider = "web"

    def capabilities(self) -> dict[str, bool]:
        return {"api_available": False, "download_available": False, "search": False, "sync": True}

    def health_check(self) -> tuple[str, str | None]:
        try:
            response = net.get(self.base_url)
        except net.RateLimited as exc:
            return "blocked", str(exc)
        except net.FetchError as exc:
            return "unreachable", str(exc)
        if response.status >= 400:
            return "error", f"HTTP {response.status}"
        return "ok", None

    def links(self, html: str) -> list[str]:
        patterns = LINK_PATTERNS.get(self.source_id, ())
        found: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                href = match.group(1)
                absolute = urljoin(self.base_url, href)
                parsed = urlparse(absolute)
                if parsed.scheme not in ("http", "https"):
                    continue
                key = absolute.rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(absolute.rstrip("/"))
                if len(found) >= _MAX_LINKS:
                    return found
        return found

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        try:
            html = net.get(self.base_url, robots=True).text
        except net.RateLimited as exc:
            report.status = "blocked"
            report.error = str(exc)
            return report
        except net.FetchError as exc:
            report.status = "unreachable"
            report.error = str(exc)
            return report
        urls = self.links(html)[: int(budget or _MAX_LINKS)]
        report.scanned = len(urls)
        if not urls:
            report.notes.append("no workflow links matched on the landing page")
        for url in urls:
            title = title_from_path(url) or url
            report.resources.append(self.resource(
                title=title,
                type_="reference",
                source_url=url,
                description="",
                metadata={"discovery_only": True,
                          "note": "the source publishes pages, not workflow files"},
            ))
        report.notes.append("link discovery only: this source publishes no workflow files")
        return report


class YouTubeProvider(BaseProvider):
    """Search-page metadata: titles, channels, descriptions. No API key, no downloads."""

    provider = "youtube"
    QUERIES = ("comfyui workflow", "comfyui wan 2.2 workflow", "comfyui lora tutorial",
               "comfyui controlnet workflow")

    def capabilities(self) -> dict[str, bool]:
        return {"api_available": False, "download_available": False, "search": True, "sync": True}

    def _search_page(self, query: str) -> str:
        url = ("https://www.youtube.com/results?search_query=" + net.quote_plus(query)
               + "&sp=EgIQAQ%253D%253D")
        return net.get(url, robots=True).text

    def health_check(self) -> tuple[str, str | None]:
        try:
            html = self._search_page("comfyui")
        except net.RateLimited as exc:
            return "blocked", str(exc)
        except net.FetchError as exc:
            return "unreachable", str(exc)
        if "ytInitialData" not in html:
            return "error", "the search page did not contain ytInitialData"
        return "ok", None

    def parse(self, html: str) -> list[dict[str, Any]]:
        """Extract video metadata from ``ytInitialData`` without executing the page."""
        match = re.search(r"ytInitialData\s*=\s*(\{.*?\});</script>", html, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return []
        videos: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                renderer = node.get("videoRenderer")
                if isinstance(renderer, dict):
                    videos.append(self._video(renderer))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return [v for v in videos if v]

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, dict):
            if isinstance(value.get("simpleText"), str):
                return value["simpleText"]
            runs = value.get("runs")
            if isinstance(runs, list):
                return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
        if isinstance(value, str):
            return value
        return ""

    def _video(self, renderer: dict[str, Any]) -> dict[str, Any]:
        video_id = renderer.get("videoId")
        title = self._text(renderer.get("title"))
        if not video_id or not title:
            return {}
        return {
            "video_id": video_id,
            "title": title,
            "channel": self._text(renderer.get("ownerText")),
            "published": self._text(renderer.get("publishedTimeText")),
            "description": self._text(renderer.get("detailedMetadataSnippets") or
                                      renderer.get("descriptionSnippet"))[:1500],
        }

    def to_resource(self, video: dict[str, Any]) -> Resource:
        description = video.get("description") or ""
        links = re.findall(r"https?://[^\s)\"']+", description)
        return self.resource(
            title=video["title"],
            type_="reference",
            description=description,
            source_url=f"https://www.youtube.com/watch?v={video['video_id']}",
            tags=["youtube", video.get("channel") or ""],
            author=video.get("channel") or "",
            metadata={"kind": "video", "video_id": video["video_id"],
                      "published": video.get("published"), "outbound_links": links[:10],
                      "discovery_only": True},
        )

    def search(self, query: str, limit: int = 20) -> list[Resource]:
        videos = self.parse(self._search_page(query))[:limit]
        return [self.to_resource(v) for v in videos]

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        per_query = int(budget or 12)
        for query in self.QUERIES:
            try:
                html = self._search_page(query)
            except net.RateLimited as exc:
                report.status = "blocked" if not report.resources else "partial"
                report.error = str(exc)
                break
            except net.FetchError as exc:
                report.status = "partial" if report.resources else "unreachable"
                report.error = str(exc)
                break
            videos = self.parse(html)[:per_query]
            report.scanned += len(videos)
            for video in videos:
                report.resources.append(self.to_resource(video))
        report.notes.append("video pages only: no downloads, no API key, descriptions may be empty")
        return report


class RedditProvider(BaseProvider):
    """Real JSON endpoint, but it needs OAuth; without it the source reports ``blocked``."""

    provider = "reddit"

    def __init__(self, spec: SourceSpec | None, **kwargs: Any) -> None:
        super().__init__(spec, **kwargs)
        self.token = (self.config or {}).get("reddit_token") or None
        self.subreddit = self._subreddit()

    def _subreddit(self) -> str:
        match = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", self.base_url)
        return match.group(1) if match else "comfyui"

    def capabilities(self) -> dict[str, bool]:
        return {"api_available": bool(self.token), "download_available": False,
                "search": bool(self.token), "sync": True}

    def health_check(self) -> tuple[str, str | None]:
        if not self.token:
            return "blocked", ("Reddit answers 403 to anonymous JSON from this network; "
                               "an OAuth token is required and not configured")
        try:
            self._listing(limit=1)
        except net.RateLimited as exc:
            return "blocked", str(exc)
        except net.FetchError as exc:
            return "error", str(exc)
        return "ok", None

    def _listing(self, *, limit: int = 25, query: str | None = None) -> dict[str, Any]:
        host = "https://oauth.reddit.com" if self.token else "https://www.reddit.com"
        path = f"/r/{self.subreddit}/search" if query else f"/r/{self.subreddit}/new"
        params = [f"limit={int(limit)}", "raw_json=1"]
        if query:
            params += [f"q={net.quote_plus(query)}", "restrict_sr=1", "sort=relevance"]
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = net.get(f"{host}{path}.json?{'&'.join(params)}", headers=headers).json()
        if not isinstance(payload, dict):
            raise net.FetchError("unexpected response from Reddit")
        return payload

    def _to_resource(self, child: dict[str, Any]) -> Resource | None:
        data = child.get("data") if isinstance(child, dict) else None
        if not isinstance(data, dict):
            return None
        permalink = data.get("permalink")
        title = data.get("title")
        if not permalink or not title:
            return None
        text = str(data.get("selftext") or "")
        links = re.findall(r"https?://[^\s)\"']+", text)
        return self.resource(
            title=str(title)[:300],
            type_="reference",
            description=text[:4000],
            source_url=f"https://www.reddit.com{permalink}",
            author=str(data.get("author") or ""),
            tags=[self.subreddit, "reddit"],
            metadata={"kind": "post", "subreddit": self.subreddit,
                      "created_utc": data.get("created_utc"),
                      "score": data.get("score"), "outbound_links": links[:10],
                      "discovery_only": True},
        )

    def search(self, query: str, limit: int = 20) -> list[Resource]:
        if not self.token:
            raise UnsupportedOperation("search", "Reddit search needs OAuth credentials")
        payload = self._listing(limit=limit, query=query)
        children = ((payload.get("data") or {}).get("children") or [])
        out = [self._to_resource(child) for child in children]
        return [r for r in out if r is not None]

    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        if not self.token:
            report.status = "blocked"
            report.error = ("Reddit answers 403 to anonymous JSON from this network; "
                            "an OAuth token is required and not configured")
            report.notes.append("the source stays registered so the limitation is visible")
            return report
        try:
            payload = self._listing(limit=int(budget or 25))
        except net.RateLimited as exc:
            report.status = "blocked"
            report.error = str(exc)
            return report
        except net.FetchError as exc:
            report.status = "error"
            report.error = str(exc)
            return report
        children = ((payload.get("data") or {}).get("children") or [])
        report.scanned = len(children)
        for child in children:
            resource = self._to_resource(child)
            if resource is None:
                report.skipped += 1
            else:
                report.resources.append(resource)
        report.notes.append("posts are leads, not proof that a workflow runs")
        return report
