"""GitHub: the only source class here that can hand us real workflow files at scale.

One instance serves one repository, whether that repository is one of the ten seeded collections or
one the user typed into the source manager. The provider is deliberately boring about budget: it
lists the tree, scores the paths that look like workflows, and fetches at most ``budget`` files per
run. Anonymous GitHub allows 60 requests an hour, so a sync that tries to read a whole monorepo would
fail halfway and lie about it; scoring plus a hard cap keeps each run small and repeatable.

Provenance is the point of this provider: every resource keeps the commit SHA it was read at, so a
later sync can tell "unchanged" from "updated" without re-downloading anything.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .. import media, net
from ..classify import classify, classify_bytes, extension_of, pick_title
from ..model import Resource
from ..sources import SourceSpec
from .base import BaseProvider, ProviderReport, UnsupportedOperation

DEFAULT_BUDGET = 40
MAX_FILE_BYTES = 3_500_000
TREE_TIMEOUT_S = 30.0

_WORKFLOW_PATH_HINT = re.compile(
    r"(workflow|example|template|vault|gallery|collection|preset|catalog)", re.IGNORECASE)
_PATH_NOISE = re.compile(r"(^|/)(\.github|node_modules|docs?/assets|tests?/fixtures)(/|$)",
                         re.IGNORECASE)


def parse_repo(base_url: str) -> tuple[str, str] | None:
    """``https://github.com/owner/name`` → ``("owner", "name")``; anything else → ``None``."""
    match = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
                     (base_url or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _path_score(path: str) -> int:
    low = path.lower()
    score = 0
    if _WORKFLOW_PATH_HINT.search(low):
        score += 3
    if extension_of(low) == ".json":
        score += 2
    if low.endswith(".png") or low.endswith(".webp"):
        score += 1
    if low.startswith("workflows/"):
        score += 2
    if _PATH_NOISE.search(low):
        score -= 5
    score -= min(low.count("/"), 6) // 2
    return score


class GitHubProvider(BaseProvider):
    provider = "github"
    api = "https://api.github.com"

    def __init__(self, spec: SourceSpec | None, *, source_id: str = "", base_url: str = "",
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(spec, source_id=source_id, base_url=base_url, config=config)
        repo = parse_repo(self.base_url) or (None, None)
        self.owner, self.name = repo if repo and repo[0] else (None, None)
        self.token = (self.config or {}).get("github_token") or None

    # -- plumbing -----------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return net.github_headers(self.token)

    def _json(self, url: str) -> Any:
        return net.get(url, headers=self._headers(), timeout=TREE_TIMEOUT_S).json()

    def capabilities(self) -> dict[str, bool]:
        caps = super().capabilities()
        caps.update({"search": False, "sync": bool(self.owner and self.name),
                     "download_available": True})
        return caps

    def health_check(self) -> tuple[str, str | None]:
        if not (self.owner and self.name):
            return "error", f"not a GitHub repository URL: {self.base_url}"
        try:
            info = self._json(f"{self.api}/repos/{self.owner}/{self.name}")
        except net.RateLimited as exc:
            return "blocked", f"rate limited by GitHub: {exc}"
        except net.FetchError as exc:
            return "error", str(exc)
        if not isinstance(info, dict):
            return "error", "unexpected response from the GitHub API"
        return "ok", None

    def repo_info(self) -> dict[str, Any]:
        info = self._json(f"{self.api}/repos/{self.owner}/{self.name}")
        if not isinstance(info, dict):
            raise net.FetchError("unexpected response from the GitHub API")
        return info

    def tree(self, ref: str) -> list[dict[str, Any]]:
        payload = self._json(
            f"{self.api}/repos/{self.owner}/{self.name}/git/trees/{quote(ref)}?recursive=1")
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise net.FetchError("the repository tree could not be read")
        return [item for item in payload["tree"] if isinstance(item, dict)]

    # -- content ------------------------------------------------------------------------
    def raw_url(self, ref: str, path: str) -> str:
        return (f"https://raw.githubusercontent.com/{self.owner}/{self.name}/{quote(ref)}/"
                f"{quote(path)}")

    def blob_url(self, ref: str, path: str) -> str:
        return f"https://github.com/{self.owner}/{self.name}/blob/{quote(ref)}/{quote(path)}"

    def _download(self, ref: str, path: str) -> bytes | None:
        try:
            response = net.get(self.raw_url(ref, path))
        except net.FetchError:
            return None
        if len(response.body) > MAX_FILE_BYTES:
            return None
        return response.body

    def collect(self, *, ref: str, path: str, repo_meta: dict[str, Any],
                commit_sha: str) -> list[Resource]:
        """Turn one repository file into zero or more resources. Never raises on bad content."""
        ext = extension_of(path)
        body = self._download(ref, path)
        if body is None:
            return []
        base = {
            "author": (repo_meta.get("owner") or {}).get("login") or self.owner or "",
            "metadata": {
                "repository": f"{self.owner}/{self.name}",
                "path": path,
                "commit_sha": commit_sha,
                "default_branch": repo_meta.get("default_branch"),
                "repository_url": repo_meta.get("html_url"),
            },
            "stats": {"stars": repo_meta.get("stargazers_count") or 0,
                      "forks": repo_meta.get("forks_count") or 0},
            "license_": _license_name(repo_meta),
        }

        if ext == ".json":
            verdict = classify_bytes(body)
            if not verdict.usable:
                return []
            return [self.resource(
                title=pick_title(verdict, path),
                description=verdict.description_hint,
                source_url=self.blob_url(ref, path), download_url=self.raw_url(ref, path),
                workflow_json=verdict.graph, workflow_format=verdict.format,
                tags=_tags_from_path(path), **base)]

        if ext in (".png", ".webp"):
            payload, image_meta = media.workflow_from_images(body)
            if payload is None:
                return []
            verdict = classify(payload)
            if not verdict.usable:
                return []
            meta = {**base["metadata"], "image_metadata": image_meta}
            return [self.resource(
                title=pick_title(verdict, path),
                description=verdict.description_hint,
                source_url=self.blob_url(ref, path), download_url=self.raw_url(ref, path),
                preview_url=self.raw_url(ref, path),
                workflow_json=verdict.graph, workflow_format=verdict.format,
                tags=_tags_from_path(path), **{**base, "metadata": meta})]

        if ext == ".zip":
            out: list[Resource] = []
            for name, payload_bytes in media.safe_zip_entries(body):
                if extension_of(name) != ".json":
                    continue
                verdict = classify_bytes(payload_bytes)
                if not verdict.usable:
                    continue
                out.append(self.resource(
                    title=pick_title(verdict, name),
                    description=verdict.description_hint,
                    source_url=self.blob_url(ref, path) + f"#{quote(name)}",
                    download_url=self.raw_url(ref, path),
                    workflow_json=verdict.graph, workflow_format=verdict.format,
                    tags=_tags_from_path(name),
                    **{**base, "metadata": {**base["metadata"], "archive": path, "entry": name}}))
                if len(out) >= 20:              # an archive is not a repository; cap the yield
                    break
            return out
        return []

    # -- sync ---------------------------------------------------------------------------
    def sync(self, *, budget: int | None = None) -> ProviderReport:
        report = ProviderReport(source_id=self.source_id)
        if not (self.owner and self.name):
            report.status = "unreachable"
            report.error = f"not a GitHub repository URL: {self.base_url}"
            return report
        limit = int(budget or DEFAULT_BUDGET)
        try:
            info = self.repo_info()
            ref = info.get("default_branch") or "main"
            tree = self.tree(ref)
        except net.RateLimited as exc:
            report.status = "blocked"
            report.error = str(exc)
            report.rate_limit = "anonymous 60/h exceeded"
            return report
        except net.FetchError as exc:
            report.status = "error"
            report.error = str(exc)
            return report

        candidates = [item for item in tree
                      if item.get("type") == "blob"
                      and extension_of(str(item.get("path") or "")) in (".json", ".png", ".webp", ".zip")
                      and not _PATH_NOISE.search(str(item.get("path") or ""))]
        candidates.sort(key=lambda item: (-_path_score(str(item.get("path"))),
                                          str(item.get("path"))))
        selected = candidates[:limit]
        report.scanned = len(selected)
        commit_sha = ref
        try:
            branch = self._json(f"{self.api}/repos/{self.owner}/{self.name}/branches/{quote(ref)}")
            if isinstance(branch, dict) and isinstance(branch.get("commit"), dict):
                commit_sha = branch["commit"].get("sha") or ref
        except net.FetchError:
            pass

        errors: list[str] = []
        for item in selected:
            path = str(item.get("path") or "")
            try:
                found = self.collect(ref=ref, path=path, repo_meta=info, commit_sha=commit_sha)
            except net.RateLimited as exc:
                report.rate_limit = str(exc)
                errors.append("GitHub rate limit reached, remaining files were not fetched")
                break
            except net.FetchError as exc:
                errors.append(str(exc))
                continue
            except Exception as exc:            # pragma: no cover - defensive for one weird file
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if not found:
                report.skipped += 1
            for resource in found:
                report.resources.append(resource)

        report.notes.append(f"{len(candidates)} candidate files, {len(selected)} inspected")
        if errors:
            report.notes.extend(errors[:5])
            report.status = "partial" if report.resources else "error"
            report.error = errors[0]
        return report

    def fetch_resource(self, external_id: str) -> Resource | None:
        raise UnsupportedOperation("fetch_resource", "identify GitHub resources by their page URL")


def _license_name(info: dict[str, Any]) -> str:
    licence = info.get("license") or {}
    if isinstance(licence, dict):
        return (licence.get("spdx_id") or licence.get("name") or "unknown") or "unknown"
    return "unknown"


def _tags_from_path(path: str) -> list[str]:
    parts = [p for p in re.split(r"[/_.-]+", path.lower()) if len(p) > 2 and not p.isdigit()]
    return parts[:8]
