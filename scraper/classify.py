"""Pure classification / tagging helpers and path utilities.

Everything here is dependency-light (config only) and side-effect free, so it is
easy to unit-test in isolation: star/size bucketing, path decomposition, vendor-
directory detection, and multi-PM detection.
"""

from __future__ import annotations

from .config import (
    EXCLUDED_DIR_NAMES,
    PM_CONFIG,
    SIZE_MID_MAX_KB,
    SIZE_SMALL_MAX_KB,
    VENDOR_SUFFIX_VARIANTS,
)


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


def _dir_of(path: str) -> str:
    """Return the directory portion of a tree path ('' for repo root)."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


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
            index.setdefault(fname, []).append(pm)  # setdefault returns the value
    return index


FILENAME_PM_INDEX = _filename_pm_index()


def detect_multi_pm(dir_basenames: set[str]) -> bool:
    """Return True if a directory contains match files for >1 distinct PM."""
    pms: set[str] = set()
    for fname in dir_basenames:
        for pm in FILENAME_PM_INDEX.get(fname, []):
            pms.add(pm)
    return len(pms) > 1


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
