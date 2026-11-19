"""Fetch a public GitHub repository's archive for a skill bundle import.

docs/specs/skill-bundle-import.md §5. The user's URL is **parsed, never
fetched**: it names an owner, a repository, an optional ref and a path, each
checked against GitHub's own character rules, and the one request made goes to
``codeload.github.com`` at a URL built here from those parts. Redirects are
off, nothing identifying is sent, and the connection goes through
``PublicOnlyTransport`` (the web crawler's resolve-then-connect check), so a
poisoned resolver cannot turn it into a request inside the VPC.

codeload rather than ``api.github.com``: one request returns the whole tree as a
zip (whose comment is the commit SHA), and it is not subject to the REST API's
60-requests-an-hour unauthenticated limit, which every user behind our one
egress address would share.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

import httpx

from apis.app_api.web_sources.public_transport import PublicOnlyTransport
from apis.shared.skills.bundle_import import MAX_GITHUB_ARCHIVE_BYTES

CODELOAD_HOST = "codeload.github.com"
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_RAW_HOST = "raw.githubusercontent.com"

# GitHub's rules: an owner is 1-39 alphanumerics or single hyphens, not leading;
# a repository name is letters, digits, '.', '_' and '-'.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# One path segment: a branch without '/', a tag or a commit SHA.
_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_PATH_SEGMENT_RE = re.compile(r"^[^\x00-\x1f\\]{1,255}$")

FETCH_TIMEOUT_SECONDS = 20.0
_TOTAL_DEADLINE_SECONDS = 30.0


class GitHubSourceError(ValueError):
    """The URL is not one we fetch, or the fetch failed. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GitHubLocation:
    owner: str
    repo: str
    ref: Optional[str]
    path: str

    @property
    def archive_url(self) -> str:
        ref = quote(self.ref or "HEAD", safe="")
        return f"https://{CODELOAD_HOST}/{quote(self.owner)}/{quote(self.repo)}/zip/{ref}"

    @property
    def display_url(self) -> str:
        base = f"https://github.com/{self.owner}/{self.repo}"
        if self.ref or self.path:
            base += f"/tree/{self.ref or 'HEAD'}"
            if self.path:
                base += f"/{self.path}"
        return base


def parse_github_url(url: str) -> GitHubLocation:
    """Read an owner, repository, ref and path out of a GitHub URL.

    Accepts ``https://github.com/{owner}/{repo}[.git]``, ``…/tree/{ref}/{path}``,
    ``…/blob/{ref}/{path}/SKILL.md`` and
    ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``.

    Raises:
        GitHubSourceError: Anything else.
    """
    text = (url or "").strip()
    if not text:
        raise GitHubSourceError("Enter a GitHub URL.")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise GitHubSourceError("That is not a valid URL.") from None
    if parts.scheme != "https":
        raise GitHubSourceError("Use an https:// GitHub URL.")
    if parts.username or parts.password or parts.port is not None:
        raise GitHubSourceError("Use a plain github.com URL.")
    host = (parts.hostname or "").lower()
    segments = [unquote(s) for s in parts.path.split("/") if s]

    if host in _GITHUB_HOSTS:
        if len(segments) < 2:
            raise GitHubSourceError("Include the repository: https://github.com/owner/repo.")
        owner, repo, rest = segments[0], segments[1], segments[2:]
        ref: Optional[str] = None
        if rest:
            if rest[0] not in ("tree", "blob") or len(rest) < 2:
                raise GitHubSourceError(
                    "Use a repository URL, or a folder's URL from GitHub "
                    "(…/tree/branch/path)."
                )
            ref, rest = rest[1], rest[2:]
    elif host == _RAW_HOST:
        if len(segments) < 3:
            raise GitHubSourceError("That raw.githubusercontent.com URL has no branch.")
        owner, repo, ref, rest = segments[0], segments[1], segments[2], segments[3:]
    else:
        raise GitHubSourceError("Only github.com and raw.githubusercontent.com URLs can be imported.")

    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in (".", ".."):
        raise GitHubSourceError("That URL does not name a GitHub repository.")
    if ref is not None and (not _REF_RE.match(ref) or ".." in ref):
        raise GitHubSourceError(
            "That branch or tag name cannot be imported by URL. Use a tag, a commit "
            "SHA, or a branch without '/' in its name."
        )
    if rest and rest[-1] == "SKILL.md":
        rest = rest[:-1]
    if any(s in (".", "..") or not _PATH_SEGMENT_RE.match(s) for s in rest):
        raise GitHubSourceError("That URL's path is not valid.")
    return GitHubLocation(owner=owner, repo=repo, ref=ref, path="/".join(rest))


def _not_found_message(location: GitHubLocation) -> str:
    """Why codeload had nothing, including the one way a valid URL reads as missing.

    A tree URL can't say where a branch name ends and the path begins, so
    ``…/tree/feature/x/skills`` is read as branch ``feature``. When a URL named both
    a ref and a path, the message says so, since that is the likely cause.
    """
    message = (
        f"{location.owner}/{location.repo}"
        f"{'@' + location.ref if location.ref else ''} was not found, "
        "or the repository is not public."
    )
    if location.ref and location.path:
        message += (
            f" If the branch name has a '/' in it, it was read as '{location.ref}': "
            "use a tag or a commit SHA in the URL instead."
        )
    return message


async def fetch_archive(
    location: GitHubLocation,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    max_bytes: int = MAX_GITHUB_ARCHIVE_BYTES,
) -> bytes:
    """Download ``location``'s repository archive, refusing past ``max_bytes``.

    ``transport`` is for tests; production always uses ``PublicOnlyTransport``.

    Raises:
        GitHubSourceError: 404 (not found or not public), 400 (too large) or
            502 (GitHub failed or timed out).
    """
    try:
        return await asyncio.wait_for(
            _download(location, transport or PublicOnlyTransport(), max_bytes),
            timeout=_TOTAL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        raise GitHubSourceError("GitHub took too long to respond. Try again.", 502) from None


async def _download(
    location: GitHubLocation, transport: httpx.AsyncBaseTransport, max_bytes: int
) -> bytes:
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        headers={"User-Agent": "skill-bundle-import", "Accept": "application/zip"},
    ) as client:
        try:
            async with client.stream("GET", location.archive_url) as response:
                if response.status_code == 404:
                    raise GitHubSourceError(_not_found_message(location), 404)
                if response.status_code != 200:
                    raise GitHubSourceError(
                        f"GitHub answered {response.status_code}. Try again later.", 502
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise _too_large(max_bytes)
                chunks = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise _too_large(max_bytes)
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            raise GitHubSourceError("Could not reach GitHub. Try again later.", 502) from None


def _too_large(max_bytes: int) -> GitHubSourceError:
    return GitHubSourceError(
        f"That repository's archive is larger than {max_bytes // (1024 * 1024)} MiB. "
        "Upload a .zip of just the skill instead."
    )
