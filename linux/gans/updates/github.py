"""Fetches releases for a GitHub repository over the public REST API (no token —
unauthenticated requests are rate-limited to 60/hour per IP, ample for a once-a-day
check). Depends only on the standard library."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, List, Optional

from ..semver import SemanticVersion

__all__ = ["GitHubRelease", "GitHubReleaseClient", "ClientError"]


class ClientError(Exception):
    @classmethod
    def bad_response(cls, code: int) -> "ClientError":
        return cls(f"GitHub returned HTTP {code}.")

    @classmethod
    def no_releases(cls) -> "ClientError":
        return cls("No published releases were found.")


@dataclass
class GitHubRelease:
    """The subset of GitHub's Releases API we care about."""

    tag_name: str
    name: Optional[str]
    body: Optional[str]
    html_url: str
    prerelease: bool
    draft: bool
    published_at: Optional[str]

    @classmethod
    def from_json(cls, data: Any) -> "GitHubRelease":
        if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str) or not isinstance(data.get("html_url"), str):
            raise ClientError("Unexpected release payload.")
        return cls(
            tag_name=data["tag_name"],
            name=data.get("name") if isinstance(data.get("name"), str) else None,
            body=data.get("body") if isinstance(data.get("body"), str) else None,
            html_url=data["html_url"],
            prerelease=bool(data.get("prerelease", False)),
            draft=bool(data.get("draft", False)),
            published_at=data.get("published_at") if isinstance(data.get("published_at"), str) else None,
        )

    def release_notes(self, max_length: int = 600) -> Optional[str]:
        """A trimmed, length-capped form of the release body, suitable for a dialog."""
        body = (self.body or "").strip()
        if not body:
            return None
        if len(body) <= max_length:
            return body
        return body[:max_length].strip() + "…"


class GitHubReleaseClient:
    def __init__(self, owner: str, repo: str, user_agent: str = "ch.lkmc.Gans"):
        self.owner = owner
        self.repo = repo
        self.user_agent = user_agent

    def latest_release(self, include_prereleases: bool) -> GitHubRelease:
        """The newest published release. When ``include_prereleases`` is false this uses the
        repo's ``releases/latest`` endpoint (which already excludes drafts and pre-releases);
        otherwise it scans the recent releases and returns the highest-versioned non-draft one."""
        if include_prereleases:
            releases = self._fetch("releases?per_page=30")
            if not isinstance(releases, list):
                raise ClientError("Unexpected release payload.")
            candidates: List[GitHubRelease] = [GitHubRelease.from_json(item) for item in releases]
            candidates = [release for release in candidates if not release.draft]
            if not candidates:
                raise ClientError.no_releases()
            return max(candidates, key=lambda release: SemanticVersion.parse(release.tag_name) or SemanticVersion.ZERO)
        return GitHubRelease.from_json(self._fetch("releases/latest"))

    def _fetch(self, path: str) -> Any:
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/{path}"
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,   # GitHub requires a User-Agent header.
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                data = response.read()
        except urllib.error.HTTPError as error:
            # 404 from the latest endpoint means the repo has no published (non-draft,
            # non-prerelease) release yet — report that rather than a raw HTTP code.
            if error.code == 404:
                raise ClientError.no_releases() from None
            raise ClientError.bad_response(error.code) from None
        except (urllib.error.URLError, OSError) as error:
            raise ClientError(f"Network error: {getattr(error, 'reason', error)}") from None
        if not 200 <= status < 300:
            raise ClientError.bad_response(status)
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ClientError(f"Couldn't read GitHub's response ({error}).") from None
