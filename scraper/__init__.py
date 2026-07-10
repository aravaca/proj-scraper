"""GitHub open-source project collector for SCA build-analyzer testing.
@author: Hyungsuk Choi, University of Maryland, 2026

This package is a collector that gathers open-source repositories from GitHub,
grouped by package manager (PM), for feeding an SCA (Software Composition
Analysis) build analyzer. Nine package managers are supported: npm, yarn,
dotnet, maven, gradle, go, composer, dart, and bundler.

Auth:
    Requires the ``GITHUB_TOKEN`` environment variable (GitHub code search needs
    an authenticated request). In PowerShell:

        $env:GITHUB_TOKEN = "<your_personal_access_token>"

Usage:
    python collect.py --pm npm --count 5      # collect 5 npm repos
    python collect.py --pm all --count 5      # every PM collects 5 repos

Requires the ``dotnet`` CLI on PATH for the dotnet special handling.

Module map:
    config.py         constants, PM_CONFIG, CSV_COLUMNS
    github_client.py  HTTP session + GitHub API request/metadata helpers
    classify.py       tagging + path/vendor helpers (pure functions)
    matching.py       match-file verification, monorepo/SDK/structure detection
    search.py         candidate discovery (search_candidates)
    archive.py        download, safe zip extraction, dotnet restore & repackage
    collector.py      per-PM orchestration (collect_for_pm, log_result)
    cli.py            argument parsing + main() entry point
"""

__all__ = ["main"]
