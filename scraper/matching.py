"""Match-file verification and repo-shape detection.

Re-verifies candidate repos against their actual git tree (rather than trusting
code search), decides monorepo vs single, detects npm workspaces, and classifies
.csproj files as SDK-style vs legacy so legacy projects can be filtered *before*
download.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .classify import _basename, is_vendor_path
from .config import PM_CONFIG
from .github_client import get_file_text, get_repo_tree


def verify_match_file(owner: str, repo: str, pm: str,
                      tree_paths: Optional[list[str]] = None,
                      branch: str = "HEAD",
                      include_vendor: bool = False) -> list[str]:
    """Return the list of match-file paths for ``pm`` in a repo.

    Re-verifies against the actual git tree rather than trusting code search.
    A repo may yield multiple paths - each maps to a separate project directory.
    Matches inside vendor/build directories (``node_modules``, ``vendor``, ...)
    are excluded via :func:`classify.is_vendor_path`, except for dotnet whose
    match file is a build artifact.

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
    content = get_file_text(owner, repo, "package.json", branch)
    if content is None:
        return False
    try:
        return "workspaces" in json.loads(content)
    except Exception:
        return False


#: Matches an SDK-style project file: <Project Sdk="..."> attribute form, or the
#: nested <Sdk .../> element form. Legacy .NET Framework csproj (packages.config
#: based) use <Project ToolsVersion=... xmlns=...> with no Sdk and are excluded.
_SDK_STYLE_RE = re.compile(r"<Project\b[^>]*\bSdk\s*=|<Sdk\b", re.IGNORECASE)


def is_sdk_style_csproj(owner: str, repo: str, path: str, branch: str) -> bool:
    """Return True if the given .csproj is SDK-style (modern ``dotnet restore``).

    Fetches the .csproj content via the Contents API and checks for an ``Sdk``
    attribute/element. Legacy .NET Framework projects (no Sdk, packages.config)
    return False so they can be filtered out *before* download/restore.
    """
    content = get_file_text(owner, repo, path, branch)
    if content is None:
        return False
    return bool(_SDK_STYLE_RE.search(content))


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
