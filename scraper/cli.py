"""Command-line interface: argument parsing and the ``main`` entry point."""

from __future__ import annotations

import sys

# Force UTF-8 for this process's text I/O so console/file output does not crash
# on non-locale-encodable bytes (e.g. cp949 on Korean Windows). Done before the
# heavier imports below so any early logging is safe. The dotnet subprocess call
# also pins its encoding explicitly.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

import argparse  # noqa: E402
import logging   # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from .collector import collect_for_pm  # noqa: E402
from .config import (  # noqa: E402
    CANDIDATE_MULTIPLIER,
    DEFAULT_OUTPUT_ROOT,
    MAX_CANDIDATE_SIZE_KB,
    PM_CONFIG,
    PMS,
)

log = logging.getLogger("collect")


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
    parser.add_argument("--max-size-kb", type=int, default=MAX_CANDIDATE_SIZE_KB,
                        help="Exclude candidate repos larger than this size in KB "
                             f"(default {MAX_CANDIDATE_SIZE_KB:,}).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help=f"Output root directory (default {DEFAULT_OUTPUT_ROOT}).")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging.")
    parser.add_argument("--candidate-multiplier", type=int, default=CANDIDATE_MULTIPLIER,
                        help=f"Multiplier for the number of candidate repos to search for (default {CANDIDATE_MULTIPLIER}).")
    return parser.parse_args(argv)


def resolve_pms(pm_arg: str) -> list[str]:
    """Resolve the --pm argument to a concrete list of PM names."""
    if pm_arg == "all":
        return list(PMS)
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
    log.info("Collecting PMs: %s (count=%d each, max_size_kb=%s)",
             ", ".join(pms), args.count, f"{args.max_size_kb:,}")

    summary: dict[str, tuple[int, int]] = {}
    for pm in pms:
        log.info("=" * 60)
        log.info("PM: %s", pm)
        try:
            success, failed = collect_for_pm(
                pm, args.count, args.min_stars, output_root, csv_path,
                max_size_kb=args.max_size_kb,
                num_of_candidate=args.count * args.candidate_multiplier,
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
    print(f"Data: {output_root / 'data'}")
    return 0
