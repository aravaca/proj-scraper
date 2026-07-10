"""Candidate discovery: turn a PM's code-search query into a filtered list of
candidate repositories (with stars/size/default_branch), before any download.
"""

from __future__ import annotations

import logging
import time

from .config import GITHUB_API, MAX_CANDIDATE_SIZE_KB, PM_CONFIG
from .github_client import get_repo_info, github_request

log = logging.getLogger("collect")


def search_candidates(pm: str, min_stars: int = 0, max_candidates: int = 60,
                      max_size_kb: int = MAX_CANDIDATE_SIZE_KB) -> list[dict]:
    """Search GitHub for candidate repos for ``pm`` before downloading any data.

    Uses the GitHub code search API with the PM's ``search_query``, de-duplicates
    to unique repositories, enriches each with stars/size/default_branch, applies
    the ``min_stars`` and ``max_size_kb`` filters, and returns up to
    ``max_candidates`` repo dicts.

    Each returned dict has: owner, repo, stars, size_kb, default_branch.
    """
    cfg = PM_CONFIG[pm]
    query = cfg["search_query"]
    log.info("[%s] searching candidates: %s", pm, query)

    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    page = 1
    per_page = 100

    while len(candidates) < max_candidates and page <= 10:
        resp = github_request(
            "GET",
            f"{GITHUB_API}/search/code",
            params={
                "q": query,
                "per_page": per_page,
                "page": page,
                "sort": "indexed",
            },
        )
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "n/a"
            body = resp.text[:200] if resp is not None else ""
            log.warning("[%s] code search page %d failed (status %s): %s",
                        pm, page, code, body)
            break

        items = resp.json().get("items", [])
        if not items:
            break

        for item in items:
            repo_obj = item.get("repository", {})
            owner = repo_obj.get("owner", {}).get("login")
            name = repo_obj.get("name")
            if not owner or not name:
                continue
            key = (owner, name)
            if key in seen:
                continue
            seen.add(key)

            info = get_repo_info(owner, name)
            if info is None:
                continue
            if info["stars"] < min_stars:
                continue
            if info["size_kb"] > max_size_kb:
                log.debug(
                    "[%s] skip %s/%s: size_kb=%d exceeds cap (%d) - too large "
                    "for the dataset",
                    pm, owner, name, info["size_kb"], max_size_kb,
                )
                continue

            candidates.append({
                "owner": owner,
                "repo": name,
                "stars": info["stars"],
                "size_kb": info["size_kb"],
                "default_branch": info["default_branch"],
            })
            if len(candidates) >= max_candidates:
                break

        page += 1
        # Code search is limited to ~30 req/min; be polite between pages.
        time.sleep(2)

    log.info("[%s] collected %d candidate repos.", pm, len(candidates))
    return candidates
