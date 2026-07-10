"""Static configuration: tunable constants, per-PM rules, and the CSV schema.

This module has no dependencies on other package modules so it can be imported
anywhere without side effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Output / sizing
# --------------------------------------------------------------------------- #

#: Root output directory. The script creates ``<OUTPUT_ROOT>/data/<pm>/``
#: subfolders and writes ``<OUTPUT_ROOT>/collection_log.csv``.
DEFAULT_OUTPUT_ROOT = Path(r"C:\Exception\Scraper")

#: Size thresholds (in KB, as reported by the GitHub repo ``size`` field which
#: is in KB). Pulled out as constants so they are trivial to retune later.
SIZE_SMALL_MAX_KB = 1_000        # <  1 MB   -> "small"
SIZE_MID_MAX_KB = 50_000         # <  50 MB  -> "mid", else "large"

#: dotnet restore timeout (seconds).
DOTNET_RESTORE_TIMEOUT = 600

#: Search for COUNT * this many candidates to yield n final repos.
CANDIDATE_MULTIPLIER = 4

#: Repos larger than this (KB, per GitHub repo ``size`` field) are excluded from
#: candidacy entirely - too large to be useful/practical for build-analyzer test
#: data, and wasteful to download (e.g. nixpkgs ~3.2GB, backstage ~5.8GB).
#: NOTE: Overridable via --max-size-kb.
MAX_CANDIDATE_SIZE_KB = 300_000   # 300 MB

# --------------------------------------------------------------------------- #
# Vendor / build-output filtering
# --------------------------------------------------------------------------- #

#: Directory names that indicate third-party / build output. A match file whose
#: path passes through any of these directories is not a real project manifest
#: (e.g. a vendored ``node_modules/**/package.json``) and is excluded.
#:
#: NOTE: this filter is for filename-mode dependency detection (npm/yarn/composer/
#: bundler/...). It is intentionally NOT applied to dotnet, whose match file is
#: literally ``obj/project.assets.json`` — see ``matching.verify_match_file``.
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

# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

#: GitHub API roots.
GITHUB_API = "https://api.github.com"    # search api = 30 req/min, core api = 5_000 req/hour
CODELOAD = "https://codeload.github.com"  # zip archive downloads, not rate limited

# --------------------------------------------------------------------------- #
# Per-PM configuration
# --------------------------------------------------------------------------- #

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

#: PMs included when ``--pm all`` is passed.
PMS = list(PM_CONFIG.keys())

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
