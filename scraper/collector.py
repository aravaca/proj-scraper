"""Per-PM orchestration: the core ``collect_for_pm`` loop that ties search,
verification, filtering, download, dotnet handling, and CSV logging together.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from .archive import dotnet_restore_and_repackage, download_zip
from .classify import _basename, _dir_of, detect_multi_pm, size_tag, star_bucket
from .config import (
    CSV_COLUMNS,
    MAX_CANDIDATE_SIZE_KB,
    MAX_REASONABLE_MATCH_COUNT,
    PM_CONFIG,
)
from .github_client import get_repo_tree
from .matching import determine_structure, is_sdk_style_csproj, verify_match_file
from .search import search_candidates

log = logging.getLogger("collect")


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


def collect_for_pm(pm: str, count: int, min_stars: int, output_root: Path,
                   csv_path: Path,
                   max_size_kb: int = MAX_CANDIDATE_SIZE_KB,
                   num_of_candidate: int = None) -> tuple[int, int]:
    """
    CORE METHOD
    Collect ``count`` projects for a single PM. Returns (success, failed).
    NOTE: num_of_candidate is by default None but this won't happen anyway [unreachable]
    """
    cfg = PM_CONFIG[pm]
    pm_dir = output_root / "data" / pm
    pm_dir.mkdir(parents=True, exist_ok=True)

    # to reduce the number of candidate search, change the value below
    candidates = search_candidates(pm, min_stars=min_stars,
                                   max_candidates=num_of_candidate,
                                   max_size_kb=max_size_kb)

    success = 0
    failed = 0

    for cand in candidates:
        if success >= count:
            break

        # Per-candidate exception isolation: a single bad candidate (e.g. a zip
        # with a Windows-illegal filename) must not abort the whole PM's loop.
        try:
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

            # --- dotnet: pre-download SDK-style filtering. -------------------
            # match_paths here are .csproj probe paths. Determine which are
            # SDK-style *before* downloading; if none are, skip the repo
            # entirely (legacy .NET Framework / packages.config projects are not
            # restorable the same way and waste bandwidth/time). The SDK-style
            # subset is what we later hand to dotnet_restore_and_repackage.
            restore_targets: list[str] = []
            if cfg["needs_build"]:
                restore_targets = [
                    p for p in match_paths
                    if is_sdk_style_csproj(owner, repo, p, branch)
                ]
                if not restore_targets:
                    log.info("[%s] skip %s/%s: no SDK-style .csproj among %d "
                             "(legacy/packages.config) - filtered before download",
                             pm, owner, repo, len(match_paths))
                    log_result({
                        **base_row,
                        "status": "skipped_legacy",
                        "fail_reason": ("no SDK-style .csproj "
                                        f"(checked {len(match_paths)})"),
                    }, csv_path)
                    continue
                log.info("[%s] %s/%s: %d/%d csproj are SDK-style -> will restore",
                         pm, owner, repo, len(restore_targets), len(match_paths))

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
                ok = dotnet_restore_and_repackage(
                    zip_path, restore_targets, info=build_info)
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

        except Exception as exc:  # isolate to this candidate; keep the loop alive
            failed += 1
            log.exception("[%s] unexpected error processing %s/%s: %s",
                          pm, cand.get("owner"), cand.get("repo"), exc)
            log_result({
                "pm": pm,
                "repo_owner": cand.get("owner", ""),
                "repo_name": cand.get("repo", ""),
                "repo_url": f"https://github.com/{cand.get('owner','')}/"
                            f"{cand.get('repo','')}",
                "status": "failed",
                "fail_reason": f"unexpected error: {exc}",
                "collected_at": _now_iso(),
            }, csv_path)
            continue

    return success, failed
