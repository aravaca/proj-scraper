"""GitHub open-source project collector for SCA build-analyzer testing.

Pilot collector that gathers open-source repositories from GitHub, grouped by
package manager (PM), for feeding an SCA (Software Composition Analysis) build
analyzer.

The pilot covers three PMs (npm / Yarn / dotnet) at 5 projects each, but the
design is deliberately config-driven: to add the remaining PMs (Maven, Gradle,
Go, Composer, Dart, Bundler) you only add an entry to ``PM_CONFIG`` - no logic
changes required.

Usage:
    python collect.py --pm npm --count 5
    python collect.py --pm all --count 5      # npm, yarn, dotnet

Auth:
    Requires the ``GITHUB_TOKEN`` environment variable (GitHub code search needs
    an authenticated request).

Requires ``dotnet`` CLI on PATH for the dotnet special handling.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: Root output directory. The script creates ``<OUTPUT_ROOT>/pilot_data/<pm>/``
#: subfolders and writes ``<OUTPUT_ROOT>/collection_log.csv``.
DEFAULT_OUTPUT_ROOT = Path(r"C:\Exception\Scraper2")

#: Size thresholds (in KB, as reported by the GitHub repo ``size`` field which
#: is in KB). Pulled out as constants so they are trivial to retune later.
SIZE_SMALL_MAX_KB = 1_000        # <  1 MB   -> "small"
SIZE_MID_MAX_KB = 50_000         # <  50 MB  -> "mid", else "large"

#: dotnet restore timeout (seconds).
DOTNET_RESTORE_TIMEOUT = 600

#: Directory names that indicate third-party / build output. A match file whose
#: path passes through any of these directories is not a real project manifest
#: (e.g. a vendored ``node_modules/**/package.json``) and is excluded.
#:
#: NOTE: this filter is for filename-mode dependency detection (npm/yarn/composer/
#: bundler/...). It is intentionally NOT applied to dotnet, whose match file is
#: literally ``obj/project.assets.json`` — see ``verify_match_file``.
EXCLUDED_DIR_NAMES = {
    "node_modules",   # npm / yarn
    "vendor",         # composer, bundler, older go
    ".git",
    "dist",
    "build",
    "bin",
    "obj",            # excluded here; dotnet matching bypasses this filter
    ".venv",
    "venv",
    "__pycache__",
}

#: Limited, explicit set of suffixes appended to a known vendor dir name that
#: still count as a vendor dir (e.g. a user renamed ``node_modules`` to
#: ``node_modules_old`` and committed it). Intentionally NOT arbitrary substring
#: matching — only ``<base><suffix>`` exact matches are excluded, so unrelated
#: names like ``node_modules-config`` or ``dist-tags`` are never caught.
VENDOR_SUFFIX_VARIANTS = [
    "", "_old", "-old", ".old", "_bak", "-bak", ".bak",
    "_backup", "-backup", ".disabled",
]

#: Second-line defense: after filtering, a project directory almost never has
#: more than this many real manifests. A higher count usually means an
#: unrecognized vendor directory slipped through; such candidates are skipped
#: for manual review. Applied to filename-mode PMs only (needs_build PMs like
#: dotnet legitimately have many .csproj files).
MAX_REASONABLE_MATCH_COUNT = 15

#: GitHub API roots.
GITHUB_API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"

#: Per-PM configuration. Adding a PM = adding an entry here.
#:
#: Keys:
#:   search_query : GitHub *code search* query used to find candidate repos.
#:   match_files  : filenames/paths that mark an analyzable project directory.
#:   match_mode   : "filename"    -> match a tree entry whose basename is in
#:                                   match_files.
#:                  "path_suffix" -> match a tree entry whose path ends with one
#:                                   of match_files (used for build artifacts
#:                                   like obj/project.assets.json).
#:   needs_build  : True if the match file is a *build artifact* not present in
#:                  the repo and must be generated locally (dotnet).
#:   build_probe  : (needs_build only) filename/extension found in the repo tree
#:                  that identifies a candidate project directory before the
#:                  build runs. For dotnet: any ``*.csproj``.
PM_CONFIG: dict[str, dict[str, Any]] = {
    "npm": {
        "search_query": "filename:package.json",
        "match_files": ["package.json"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "yarn": {
        "search_query": "filename:yarn.lock",
        "match_files": ["yarn.lock"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "dotnet": {
        "search_query": "extension:csproj",
        "match_files": ["obj/project.assets.json"],
        "match_mode": "path_suffix",
        "needs_build": True,
        "build_probe_ext": ".csproj",
    },
    # --- Expansion PMs (not part of the pilot; enable by passing --pm <name>) ---
    "maven": {
        "search_query": "filename:pom.xml",
        "match_files": ["pom.xml"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "gradle": {
        "search_query": "filename:build.gradle",
        "match_files": ["build.gradle", "build.gradle.kts"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "go": {
        "search_query": "filename:go.mod",
        "match_files": ["go.mod"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "composer": {
        "search_query": "filename:composer.lock",
        "match_files": ["composer.lock"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "dart": {
        "search_query": "filename:pubspec.yaml",
        "match_files": ["pubspec.yaml"],
        "match_mode": "filename",
        "needs_build": False,
    },
    "bundler": {
        "search_query": "filename:Gemfile.lock",
        "match_files": ["Gemfile.lock"],
        "match_mode": "filename",
        "needs_build": False,
    },
}

#: PMs included when ``--pm all`` is passed (the pilot set).
PILOT_PMS = ["npm", "yarn", "dotnet"]

#: CSV column order.
CSV_COLUMNS = [
    "pm",
    "repo_owner",
    "repo_name",
    "repo_url",
    "match_file_paths",
    "match_count",
    "stars",
    "star_bucket",
    "size_kb",
    "size_tag",
    "structure",
    "multi_pm",
    "dotnet_sdk_version",
    "status",
    "fail_reason",
    "collected_at",
]

log = logging.getLogger("collect")

# --------------------------------------------------------------------------- #
# HTTP session / GitHub request helpers
# --------------------------------------------------------------------------- #


def build_session() -> requests.Session:
    """Create a requests session pre-loaded with GitHub auth + UA headers."""
    session = requests.Session()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sca-pilot-collector",
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


# --------------------------------------------------------------------------- #
# Classification helpers
# --------------------------------------------------------------------------- #


def star_bucket(stars: int) -> str:
    """Bucket a star count into '0-100' / '100-1000' / '1000+'."""
    if stars < 100:
        return "0-100"
    if stars < 1000:
        return "100-1000"
    return "1000+"


def size_tag(size_kb: int) -> str:
    """Tag a repo size (KB) into 'small' / 'mid' / 'large'."""
    if size_kb < SIZE_SMALL_MAX_KB:
        return "small"
    if size_kb < SIZE_MID_MAX_KB:
        return "mid"
    return "large"


def _filename_pm_index() -> dict[str, list[str]]:
    """Map each filename-mode match file -> list of PMs that claim it.

    Used for multi-PM detection within a single directory. Only filename-mode
    PMs participate (build-artifact PMs like dotnet are excluded because their
    match file is not present in the source tree).
    """
    index: dict[str, list[str]] = {}
    for pm, cfg in PM_CONFIG.items():
        if cfg["match_mode"] != "filename":
            continue
        for fname in cfg["match_files"]:
            index.setdefault(fname, []).append(pm)
    return index


FILENAME_PM_INDEX = _filename_pm_index()


def detect_multi_pm(dir_basenames: set[str]) -> bool:
    """Return True if a directory contains match files for >1 distinct PM."""
    pms: set[str] = set()
    for fname in dir_basenames:
        for pm in FILENAME_PM_INDEX.get(fname, []):
            pms.add(pm)
    return len(pms) > 1


# --------------------------------------------------------------------------- #
# Repo metadata + tree
# --------------------------------------------------------------------------- #


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


def _dir_of(path: str) -> str:
    """Return the directory portion of a tree path ('' for repo root)."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


#: Precomputed set of every allowed vendor dir name incl. suffix variants,
#: lowercased for case-insensitive exact-match lookup.
_VENDOR_DIR_VARIANTS = frozenset(
    f"{base}{suffix}".lower()
    for base in EXCLUDED_DIR_NAMES
    for suffix in VENDOR_SUFFIX_VARIANTS
)


def is_vendor_path(path: str) -> bool:
    """Return True if a repo path lives inside a vendor/build/output directory.

    Matches on exact path components (not substrings), allowing a limited set of
    known rename/backup suffixes (:data:`VENDOR_SUFFIX_VARIANTS`), so
    ``node_modules/x`` and ``node_modules_old/x`` are excluded but unrelated
    names like ``node_modules-config/x`` are not. Case-insensitive. Shared
    across all dependency-detection PMs; not used for dotnet build-artifact
    matching.
    """
    return any(part.lower() in _VENDOR_DIR_VARIANTS for part in path.split("/"))


# --------------------------------------------------------------------------- #
# Candidate search
# --------------------------------------------------------------------------- #


def search_candidates(pm: str, min_stars: int = 0, max_candidates: int = 60) -> list[dict]:
    """Search GitHub for candidate repos for ``pm``.

    Uses the GitHub code search API with the PM's ``search_query``, de-duplicates
    to unique repositories, enriches each with stars/size/default_branch, applies
    the ``min_stars`` filter, and returns up to ``max_candidates`` repo dicts.

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


# --------------------------------------------------------------------------- #
# Match-file verification
# --------------------------------------------------------------------------- #


def verify_match_file(owner: str, repo: str, pm: str,
                      tree_paths: Optional[list[str]] = None,
                      branch: str = "HEAD",
                      include_vendor: bool = False) -> list[str]:
    """Return the list of match-file paths for ``pm`` in a repo.

    Re-verifies against the actual git tree rather than trusting code search.
    A repo may yield multiple paths - each maps to a separate project directory.
    Matches inside vendor/build directories (``node_modules``, ``vendor``, ...)
    are excluded via :func:`is_vendor_path`, except for dotnet whose match file
    is a build artifact.

    For build-artifact PMs (``needs_build``, e.g. dotnet) the match file does
    not exist in the source tree, so this returns the *build probe* paths
    (e.g. ``*.csproj``) that mark candidate project directories to build.

    Args:
        tree_paths: pre-fetched tree (avoids a duplicate API call); fetched if
            omitted.
        branch: branch to fetch the tree from when ``tree_paths`` is omitted.
        include_vendor: if True, skip the vendor-path filter (used for
            diagnostics — e.g. detecting "vendor-only" repos).
    """
    cfg = PM_CONFIG[pm]
    if tree_paths is None:
        tree_paths, _ = get_repo_tree(owner, repo, branch)

    def _keep(path: str) -> bool:
        return include_vendor or not is_vendor_path(path)

    matches: list[str] = []

    if cfg["needs_build"]:
        # dotnet: the match file is a build artifact (obj/project.assets.json);
        # the vendor filter is intentionally NOT applied here.
        ext = cfg["build_probe_ext"]
        matches = [p for p in tree_paths if p.endswith(ext)]
    elif cfg["match_mode"] == "filename":
        wanted = set(cfg["match_files"])
        matches = [p for p in tree_paths if _basename(p) in wanted and _keep(p)]
    elif cfg["match_mode"] == "path_suffix":
        suffixes = cfg["match_files"]
        matches = [p for p in tree_paths
                   if any(p.endswith(s) for s in suffixes) and _keep(p)]

    return sorted(matches)


def has_npm_workspaces(owner: str, repo: str, branch: str) -> bool:
    """Return True if the root package.json declares a ``workspaces`` field."""
    resp = github_request(
        "GET",
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/package.json",
        params={"ref": branch},
    )
    if resp is None or resp.status_code != 200:
        return False
    import base64
    import json as _json
    try:
        content = base64.b64decode(resp.json().get("content", "")).decode("utf-8")
        return "workspaces" in _json.loads(content)
    except Exception:
        return False


def determine_structure(owner: str, repo: str, pm: str, branch: str,
                        match_dirs: set[str]) -> str:
    """Classify repo as 'single' or 'monorepo'.

    Monorepo if the match file appears in more than one directory, or (npm) the
    root package.json declares workspaces.
    """
    if len(match_dirs) > 1:
        return "monorepo"
    if pm == "npm" and has_npm_workspaces(owner, repo, branch):
        return "monorepo"
    return "single"


# --------------------------------------------------------------------------- #
# Download + dotnet handling
# --------------------------------------------------------------------------- #


def download_zip(owner: str, repo: str, dest: Path, branch: str = "main",
                 max_retries: int = 3) -> Optional[Path]:
    """Download a repo's default-branch zip via codeload, with retries.

    Returns the written path, or ``None`` after ``max_retries`` failures.
    """
    url = f"{CODELOAD}/{owner}/{repo}/zip/refs/heads/{branch}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        resp = github_request("GET", url, stream=True)
        if resp is not None and resp.status_code == 200:
            try:
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
                log.info("downloaded %s/%s -> %s", owner, repo, dest.name)
                return dest
            except (OSError, requests.RequestException) as exc:
                log.warning("write error for %s/%s (%s) [attempt %d/%d]",
                            owner, repo, exc, attempt, max_retries)
        else:
            code = resp.status_code if resp is not None else "n/a"
            log.warning("download failed for %s/%s (status %s) [attempt %d/%d]",
                        owner, repo, code, attempt, max_retries)
        time.sleep(2 * attempt)

    return None


def _read_global_json_sdk(root: Path) -> Optional[str]:
    """Return the SDK version pinned by any global.json under ``root``, if any."""
    import json as _json
    for gj in root.rglob("global.json"):
        try:
            data = _json.loads(gj.read_text(encoding="utf-8"))
            version = data.get("sdk", {}).get("version")
            if version:
                return version
        except Exception:
            continue
    return None


def _rezip_dir(src_dir: Path, zip_path: Path) -> None:
    """Re-zip the contents of ``src_dir`` (including generated obj/) to zip_path."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir.parent))


def dotnet_restore_and_repackage(zip_path: Path, info: Optional[dict] = None) -> bool:
    """Extract, ``dotnet restore``, verify obj/project.assets.json, and re-zip.

    Steps (per requirement 6):
      1. Extract the downloaded zip.
      2. If a global.json exists, record the required SDK version.
      3. Run ``dotnet restore`` (subprocess, with a timeout).
      4. On failure, record the reason and return False (caller skips).
      5. On success, verify obj/project.assets.json exists, then re-zip the
         directory *in place* so the built state is preserved.

    Args:
        zip_path: the downloaded repo zip (overwritten with the built state).
        info: optional mutable dict; populated with ``dotnet_sdk_version`` and
            ``fail_reason`` for the CSV row. (Kept as an optional param so the
            documented ``-> bool`` signature is preserved.)

    Returns:
        True if restore succeeded and project.assets.json was produced.
    """
    info = info if info is not None else {}
    work_dir = zip_path.with_suffix("")  # extraction dir alongside the zip
    if work_dir.exists():
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work_dir)
    except zipfile.BadZipFile as exc:
        info["fail_reason"] = f"bad zip: {exc}"
        return False

    # codeload zips contain a single top-level folder; restore from there.
    subdirs = [p for p in work_dir.iterdir() if p.is_dir()]
    restore_root = subdirs[0] if len(subdirs) == 1 else work_dir

    sdk_version = _read_global_json_sdk(restore_root)
    if sdk_version:
        info["dotnet_sdk_version"] = sdk_version
        log.info("global.json pins SDK version %s", sdk_version)

    try:
        proc = subprocess.run(
            ["dotnet", "restore"],
            cwd=str(restore_root),
            capture_output=True,
            text=True,
            timeout=DOTNET_RESTORE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        info["fail_reason"] = f"dotnet restore timed out (>{DOTNET_RESTORE_TIMEOUT}s)"
        return False
    except FileNotFoundError:
        info["fail_reason"] = "dotnet CLI not found on PATH"
        return False

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        info["fail_reason"] = "dotnet restore failed: " + " | ".join(tail)[:400]
        return False

    assets = list(restore_root.rglob("obj/project.assets.json"))
    if not assets:
        info["fail_reason"] = "restore succeeded but no obj/project.assets.json produced"
        return False

    _rezip_dir(restore_root, zip_path)
    log.info("dotnet restore OK - %d project.assets.json produced.", len(assets))
    return True


# --------------------------------------------------------------------------- #
# Logging results
# --------------------------------------------------------------------------- #


def log_result(row: dict, csv_path: Path) -> None:
    """Append a result ``row`` to the CSV, writing the header if new."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Per-PM collection
# --------------------------------------------------------------------------- #


def collect_for_pm(pm: str, count: int, min_stars: int, output_root: Path,
                   csv_path: Path) -> tuple[int, int]:
    """Collect ``count`` projects for a single PM. Returns (success, failed)."""
    cfg = PM_CONFIG[pm]
    pm_dir = output_root / "pilot_data" / pm
    pm_dir.mkdir(parents=True, exist_ok=True)

    candidates = search_candidates(pm, min_stars=min_stars,
                                   max_candidates=max(count * 12, 30))

    success = 0
    failed = 0

    for cand in candidates:
        if success >= count:
            break

        owner, repo = cand["owner"], cand["repo"]
        branch = cand["default_branch"]
        repo_url = f"https://github.com/{owner}/{repo}"

        tree_paths, truncated = get_repo_tree(owner, repo, branch)
        if truncated:
            log.warning("%s/%s tree truncated - match detection is partial.",
                        owner, repo)

        match_paths = verify_match_file(owner, repo, pm, tree_paths=tree_paths)
        if not match_paths:
            # Distinguish "no manifest at all" from "only vendored manifests"
            # (all matches filtered out) for easier debugging.
            raw = verify_match_file(owner, repo, pm, tree_paths=tree_paths,
                                    include_vendor=True)
            if raw:
                log.info("[%s] skip %s/%s: only vendor-path matches found "
                         "(0 valid after filtering)", pm, owner, repo)
            else:
                log.info("[%s] %s/%s: no match file - skipping.", pm, owner, repo)
            continue

        # Second-line defense: an implausibly large match count (for non-build
        # PMs) usually means an unrecognized vendor directory slipped past the
        # name filter. Skip and log for manual review instead of ingesting it.
        if not cfg["needs_build"] and len(match_paths) > MAX_REASONABLE_MATCH_COUNT:
            log.warning(
                "[%s] skip %s/%s: match_count=%d exceeds sanity threshold (%d) "
                "- likely unrecognized vendor directory, manual review needed",
                pm, owner, repo, len(match_paths), MAX_REASONABLE_MATCH_COUNT,
            )
            log_result({
                "pm": pm,
                "repo_owner": owner,
                "repo_name": repo,
                "repo_url": repo_url,
                "match_file_paths": ";".join(match_paths),
                "match_count": len(match_paths),
                "stars": cand["stars"],
                "star_bucket": star_bucket(cand["stars"]),
                "size_kb": cand["size_kb"],
                "size_tag": size_tag(cand["size_kb"]),
                "status": "skipped_suspicious",
                "fail_reason": (f"match_count {len(match_paths)} > "
                                f"{MAX_REASONABLE_MATCH_COUNT}"),
                "collected_at": _now_iso(),
            }, csv_path)
            continue

        match_dirs = {_dir_of(p) for p in match_paths}
        structure = determine_structure(owner, repo, pm, branch, match_dirs)

        # Precompute directory -> basenames for multi_pm detection.
        dir_basenames: dict[str, set[str]] = {}
        for p in tree_paths:
            dir_basenames.setdefault(_dir_of(p), set()).add(_basename(p))

        # Option A (pilot): one repo = one project = one count. All match paths
        # are recorded in the log for later (Option B) monorepo splitting; the
        # repo is flagged multi_pm if *any* match directory holds >1 PM's files.
        multi_pm = any(
            detect_multi_pm(dir_basenames.get(d, set())) for d in match_dirs
        )

        base_row = {
            "pm": pm,
            "repo_owner": owner,
            "repo_name": repo,
            "repo_url": repo_url,
            "match_file_paths": ";".join(match_paths),
            "match_count": len(match_paths),
            "stars": cand["stars"],
            "star_bucket": star_bucket(cand["stars"]),
            "size_kb": cand["size_kb"],
            "size_tag": size_tag(cand["size_kb"]),
            "structure": structure,
            "multi_pm": multi_pm,
            "dotnet_sdk_version": "",
            "collected_at": _now_iso(),
        }

        # --- Download the repo zip once. ---
        zip_path = pm_dir / f"{pm}_{owner}_{repo}.zip"
        got = download_zip(owner, repo, zip_path, branch=branch)
        if got is None:
            failed += 1
            row = dict(base_row)
            row.update({
                "status": "failed",
                "fail_reason": "download failed after retries",
            })
            log_result(row, csv_path)
            continue

        # --- dotnet special handling: build + verify artifact + repackage. ---
        if cfg["needs_build"]:
            build_info: dict = {}
            ok = dotnet_restore_and_repackage(zip_path, info=build_info)
            base_row["dotnet_sdk_version"] = build_info.get("dotnet_sdk_version", "")
            if not ok:
                failed += 1
                row = dict(base_row)
                row.update({
                    "status": "failed",
                    "fail_reason": build_info.get("fail_reason", "dotnet restore failed"),
                })
                log_result(row, csv_path)
                log.info("[%s] %s/%s build failed: %s",
                         pm, owner, repo, row["fail_reason"])
                continue

        # --- One repo = one project = one count (Option A). ---
        row = dict(base_row)
        row.update({"status": "success", "fail_reason": ""})
        log_result(row, csv_path)
        success += 1
        log.info(
            "[%s] +1 (%d/%d) %s/%s :: %d matches (%s)%s",
            pm, success, count, owner, repo,
            len(match_paths), ", ".join(match_paths),
            " [multi_pm]" if multi_pm else "",
        )

    return success, failed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect GitHub projects per package manager for SCA testing."
    )
    parser.add_argument(
        "--pm", required=True,
        help="Package manager to collect: one of "
             + ", ".join(PM_CONFIG) + ", or 'all' (npm, yarn, dotnet).",
    )
    parser.add_argument("--count", type=int, default=5,
                        help="Number of projects to collect per PM (default 5).")
    parser.add_argument("--min-stars", type=int, default=0,
                        help="Minimum star count filter (default 0).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help=f"Output root directory (default {DEFAULT_OUTPUT_ROOT}).")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging.")
    return parser.parse_args(argv)


def resolve_pms(pm_arg: str) -> list[str]:
    """Resolve the --pm argument to a concrete list of PM names."""
    if pm_arg == "all":
        return list(PILOT_PMS)
    if pm_arg not in PM_CONFIG:
        raise SystemExit(
            f"Unknown --pm '{pm_arg}'. Choose from: {', '.join(PM_CONFIG)}, all."
        )
    return [pm_arg]


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    pms = resolve_pms(args.pm)
    output_root: Path = args.out
    csv_path = output_root / "collection_log.csv"
    output_root.mkdir(parents=True, exist_ok=True)

    log.info("Output root: %s", output_root)
    log.info("Collecting PMs: %s (count=%d each)", ", ".join(pms), args.count)

    summary: dict[str, tuple[int, int]] = {}
    for pm in pms:
        log.info("=" * 60)
        log.info("PM: %s", pm)
        try:
            success, failed = collect_for_pm(
                pm, args.count, args.min_stars, output_root, csv_path
            )
        except Exception as exc:  # keep the whole run alive per requirement 9
            log.exception("[%s] unexpected error: %s", pm, exc)
            success, failed = 0, 0
        summary[pm] = (success, failed)

    # --- Console summary. ---
    print("\n" + "=" * 48)
    print("Collection summary")
    print("=" * 48)
    for pm, (success, failed) in summary.items():
        print(f"  {pm:<10} success={success:<3} failed={failed}")
    print(f"\nLog: {csv_path}")
    print(f"Data: {output_root / 'pilot_data'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
