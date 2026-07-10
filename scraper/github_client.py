"""GitHub HTTP client: authenticated session, rate-limit-aware requests, and
low-level repo metadata / tree / file-content fetchers.

A single module-level :data:`SESSION` carries auth headers; it is built once at
import time.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional

import requests

from .config import GITHUB_API

log = logging.getLogger("collect")


def build_session() -> requests.Session:
    """Create a requests session pre-loaded with GitHub auth + UA headers."""
    session = requests.Session()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "proj-collector",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        log.warning(
            "GITHUB_TOKEN is not set. Code search requires authentication and "
            "unauthenticated requests are heavily rate limited - expect failures."
        )
    session.headers.update(headers)
    return session


SESSION = build_session()


def _sleep_until(reset_ts: float) -> None:
    """Sleep until a unix ``reset_ts`` (plus a small buffer)."""
    wait = max(0.0, reset_ts - time.time()) + 2
    log.warning("Rate limit hit - sleeping %.0fs until reset.", wait)
    time.sleep(wait)


def github_request(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    max_retries: int = 3,
    stream: bool = False,
) -> Optional[requests.Response]:
    """Perform a GitHub API request with rate-limit + transient-error handling.

    Handles:
      * 403 with ``X-RateLimit-Remaining: 0`` -> waits until reset then retries.
      * secondary rate limits (``Retry-After`` header) -> waits then retries.
      * 5xx and network errors -> exponential backoff, up to ``max_retries``.

    Returns the response, or ``None`` if all retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = SESSION.request(
                method, url, params=params, timeout=60, stream=stream
            )
        except requests.RequestException as exc:
            log.warning(
                "Network error on %s (%s) [attempt %d/%d]",
                url, exc, attempt, max_retries,
            )
            time.sleep(2 * attempt)
            continue

        remaining = resp.headers.get("X-RateLimit-Remaining")

        # Primary rate limit exhausted.
        if resp.status_code == 403 and remaining == "0":
            reset = float(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            _sleep_until(reset)
            continue

        # Secondary / abuse rate limit.
        if resp.status_code in (403, 429) and resp.headers.get("Retry-After"):
            retry_after = int(resp.headers["Retry-After"])
            log.warning("Secondary rate limit - sleeping %ds.", retry_after + 1)
            time.sleep(retry_after + 1)
            continue

        # Transient server errors.
        if resp.status_code in (500, 502, 503, 504):
            log.warning(
                "Server error %d on %s [attempt %d/%d]",
                resp.status_code, url, attempt, max_retries,
            )
            time.sleep(2 * attempt)
            continue

        return resp

    log.error("Giving up on %s after %d attempts.", url, max_retries)
    return None


def get_repo_info(owner: str, repo: str) -> Optional[dict]:
    """Fetch repo metadata (default_branch, stars, size) or None if unavailable."""
    resp = github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}")
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "n/a"
        log.warning("repo info failed for %s/%s (status %s)", owner, repo, code)
        return None
    data = resp.json()
    return {
        "default_branch": data.get("default_branch", "main"),
        "stars": int(data.get("stargazers_count", 0)),
        "size_kb": int(data.get("size", 0)),
    }


def get_repo_tree(owner: str, repo: str, branch: str) -> tuple[list[str], bool]:
    """Return (list of all file paths in the repo, truncated_flag).

    Uses the recursive git tree API. ``truncated`` is True when GitHub capped
    the tree (very large repos); callers should treat results as best-effort.
    """
    resp = github_request(
        "GET",
        f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if resp is None or resp.status_code != 200:
        return [], False
    data = resp.json()
    paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
    return paths, bool(data.get("truncated"))


def get_file_text(owner: str, repo: str, path: str, branch: str) -> Optional[str]:
    """Fetch a single file's text content via the GitHub Contents API.

    Returns the decoded UTF-8 text, or None if the file is unavailable (missing,
    too large for the contents API, non-text, or a request failure).
    """
    resp = github_request(
        "GET",
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
        params={"ref": branch},
    )
    if resp is None or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        if data.get("encoding") != "base64" or "content" not in data:
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None
