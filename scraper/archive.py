"""Archive lifecycle: download a repo zip, extract it safely on Windows, and
(for dotnet) run ``dotnet restore`` on a specific .csproj then repackage.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Optional

import requests

from .config import CODELOAD, DOTNET_RESTORE_TIMEOUT
from .github_client import github_request

log = logging.getLogger("collect")


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
    for gj in root.rglob("global.json"):
        try:
            data = json.loads(gj.read_text(encoding="utf-8"))
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


#: Characters not allowed in Windows filenames: control chars (0x00-0x1f) and
#: the reserved set < > : " | ? *.
_WINDOWS_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')


def _sanitize_path_component(name: str) -> str:
    """Strip characters illegal in Windows filenames from a single path segment,
    and strip trailing dots/spaces (also illegal on Windows)."""
    cleaned = _WINDOWS_ILLEGAL_CHARS.sub("", name)
    cleaned = cleaned.rstrip(" .")
    return cleaned or "_"  # never produce an empty segment


def safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip archive to ``dest``, sanitizing each path component so the
    result is always a valid Windows path.

    Source repos (often authored on Linux) may contain filenames with control
    characters or other bytes that are illegal on Windows - e.g. a trailing
    ``\\r`` in a filename - which make ``zipfile.extractall`` raise OSError.
    """
    for zinfo in zf.infolist():
        parts = [p for p in zinfo.filename.split("/") if p not in ("", ".")]
        safe_parts = [_sanitize_path_component(p) for p in parts]
        if not safe_parts:
            continue
        target = dest.joinpath(*safe_parts)
        if zinfo.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(zinfo) as src, open(target, "wb") as dst:
            dst.write(src.read())


def dotnet_restore_and_repackage(zip_path: Path, csproj_rel_paths: list[str],
                                 info: Optional[dict] = None) -> bool:
    """Extract, ``dotnet restore`` a specific .csproj, verify its assets, re-zip.

    Steps:
      1. Extract the downloaded zip (sanitizing illegal filenames).
      2. If a global.json exists, record the required SDK version.
      3. For each candidate .csproj (in the given order - the caller passes only
         SDK-style ones), run ``dotnet restore <that .csproj>`` explicitly. This
         avoids MSB1011 ("multiple project files") that occurs when restoring a
         directory that contains more than one project file. The first .csproj
         that restores AND produces obj/project.assets.json is accepted.
      4. If none succeed, record the reason and return False (caller skips).
      5. On success, re-zip the directory *in place* so the built state is kept.

    Args:
        zip_path: the downloaded repo zip (overwritten with the built state).
        csproj_rel_paths: repo-root-relative paths of the .csproj files to try,
            in preference order (caller pre-filters to SDK-style).
        info: optional mutable dict; populated with ``dotnet_sdk_version``,
            ``restored_csproj`` and ``fail_reason`` for the CSV row.

    Returns:
        True if some .csproj restored and produced project.assets.json.
    """
    info = info if info is not None else {}
    work_dir = zip_path.with_suffix("")  # extraction dir alongside the zip
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    success = False
    try:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                safe_extractall(zf, work_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            info["fail_reason"] = f"extraction failed: {exc}"
            return False

        # codeload zips contain a single top-level folder; restore from there.
        subdirs = [p for p in work_dir.iterdir() if p.is_dir()]
        restore_root = subdirs[0] if len(subdirs) == 1 else work_dir

        sdk_version = _read_global_json_sdk(restore_root)
        if sdk_version:
            info["dotnet_sdk_version"] = sdk_version
            log.info("global.json pins SDK version %s", sdk_version)

        if not csproj_rel_paths:
            info["fail_reason"] = "no SDK-style .csproj to restore"
            return False

        # Try each candidate .csproj explicitly; first full success wins.
        last_reason = "no csproj attempted"
        for rel in csproj_rel_paths:
            target = restore_root / Path(rel)
            if not target.exists():
                last_reason = f"csproj not found after extract: {rel}"
                continue

            try:
                proc = subprocess.run(
                    ["dotnet", "restore", str(target)],
                    cwd=str(restore_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",   # don't rely on the OS locale (cp949 on
                    errors="replace",   # Korean Windows) - dotnet emits UTF-8
                    timeout=DOTNET_RESTORE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                last_reason = f"dotnet restore timed out (>{DOTNET_RESTORE_TIMEOUT}s) on {rel}"
                continue
            except FileNotFoundError:
                # dotnet missing is fatal for every candidate - stop early.
                info["fail_reason"] = "dotnet CLI not found on PATH"
                return False

            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
                last_reason = f"restore failed on {rel}: " + " | ".join(tail)[:300]
                continue

            # Verify this project's own assets file was produced.
            assets = list((target.parent / "obj").glob("project.assets.json"))
            if not assets:
                last_reason = f"restore ok but no obj/project.assets.json for {rel}"
                continue

            info["restored_csproj"] = rel
            log.info("dotnet restore OK - %s -> %s", rel, assets[0].name)
            _rezip_dir(restore_root, zip_path)
            success = True
            return True

        info["fail_reason"] = f"all {len(csproj_rel_paths)} csproj restores failed; last: {last_reason}"
        return False
    finally:
        # Always remove the extraction folder. On failure, also delete the
        # downloaded zip so no trace of a rejected candidate is left in the
        # output directory (CSV keeps the failure row for traceability).
        shutil.rmtree(work_dir, ignore_errors=True)
        if not success:
            zip_path.unlink(missing_ok=True)
