"""CLI entry point — argparse subcommands for the energy-baseline toolkit."""

from __future__ import annotations

import argparse
import logging
import sys

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """Add common options shared by data subcommands."""
    p.add_argument(
        "--data-dir",
        default=None,
        help="Data directory (default: $ENERGY_BASELINE_DATA_DIR or ~/data/energy-baseline)",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to tariff config YAML (default: config/tariff.yaml)",
    )
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="energy-baseline",
        description="Reproducible energy measurement and baseline toolkit (ADR-0003)",
    )
    subs = parser.add_subparsers(dest="command")

    # -- extract ----------------------------------------------------------
    p_extract = subs.add_parser("extract", help="Ingest source data to raw Parquet")
    _add_common(p_extract)
    p_extract.add_argument(
        "--source",
        required=True,
        choices=["prometheus", "ha-history", "all"],
        help="Data source to extract from",
    )
    p_extract.add_argument("--start", required=True, help="Start time (ISO 8601 UTC)")
    p_extract.add_argument("--end", required=True, help="End time (ISO 8601 UTC)")
    p_extract.add_argument(
        "--entities",
        action="append",
        default=None,
        help="Entity IDs — repeatable or comma-separated",
    )

    # -- validate ---------------------------------------------------------
    p_validate = subs.add_parser("validate", help="Validate raw Parquet against schemas")
    _add_common(p_validate)

    # -- build ------------------------------------------------------------
    p_build = subs.add_parser("build", help="Build 15-min analysis dataset")
    _add_common(p_build)
    p_build.add_argument("--start", required=True, help="Start time (ISO 8601 UTC)")
    p_build.add_argument("--end", required=True, help="End time (ISO 8601 UTC)")

    # -- benchmark --------------------------------------------------------
    p_bench = subs.add_parser("benchmark", help="Run policy benchmarks on a dataset")
    _add_common(p_bench)
    p_bench.add_argument("--dataset", required=True, help="Path to analysis dataset directory")
    p_bench.add_argument(
        "--policies",
        default="all",
        help="Comma-separated policy names or 'all' (default: all)",
    )

    # -- report -----------------------------------------------------------
    p_report = subs.add_parser("report", help="Generate monthly reconciliation report")
    _add_common(p_report)

    # -- inspect-sql ------------------------------------------------------
    p_sql = subs.add_parser("inspect-sql", help="Inspect DuckDB SQL queries")
    _add_common(p_sql)

    # -- mirror (deferred) ------------------------------------------------
    p_mirror = subs.add_parser("mirror", help="Mirror dataset to SQL (deferred in Phase 1)")
    _add_common(p_mirror)

    return parser


# ---------------------------------------------------------------------------
# Entity parsing
# ---------------------------------------------------------------------------


def parse_entities(values: list[str] | None) -> list[str]:
    """Flatten repeatable / comma-separated entity arguments."""
    if not values:
        return []
    result: list[str] = []
    for v in values:
        result.extend(e.strip() for e in v.split(",") if e.strip())
    return result


# ---------------------------------------------------------------------------
# Command handlers (stubs raise NotImplementedError)
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract source data to raw Parquet."""
    from energy_baseline.extract_ha import extract_ha_history
    from energy_baseline.extract_prom import extract_prometheus

    entities = parse_entities(args.entities)
    source: str = args.source

    if source in ("prometheus", "all"):
        extract_prometheus(
            data_dir=args.data_dir,
            config_path=args.config,
            start=args.start,
            end=args.end,
            entities=entities,
        )
    if source in ("ha-history", "all"):
        extract_ha_history(
            data_dir=args.data_dir,
            config_path=args.config,
            start=args.start,
            end=args.end,
            entities=entities,
        )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate raw Parquet against schemas."""
    raise NotImplementedError("validate is not yet implemented")


def cmd_build(args: argparse.Namespace) -> int:
    """Build 15-min analysis dataset."""
    from energy_baseline.build import build_dataset

    build_dataset(
        data_dir=args.data_dir,
        config_path=args.config,
        start=args.start,
        end=args.end,
    )
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run policy benchmarks on a dataset."""
    raise NotImplementedError("benchmark is not yet implemented")


def cmd_report(args: argparse.Namespace) -> int:
    """Generate monthly reconciliation report."""
    from energy_baseline.report import generate_report

    generate_report(
        data_dir=args.data_dir,
        config_path=args.config,
    )
    return 0


def cmd_inspect_sql(args: argparse.Namespace) -> int:
    """Inspect DuckDB SQL queries."""
    raise NotImplementedError("inspect-sql is not yet implemented")


def cmd_mirror(args: argparse.Namespace) -> int:
    """Mirror dataset to SQL — deferred in Phase 1."""
    print(
        "mirror: deferred — not implemented in Phase 1 (ADR-0003 §optional).\n"
        "The optional TimescaleDB / SQL mirror is out of scope for this phase.\n"
        "See ADR-0003 'Scope boundary' table and 'Rejected or deferred alternatives'."
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_HANDLERS = {
    "extract": cmd_extract,
    "validate": cmd_validate,
    "build": cmd_build,
    "benchmark": cmd_benchmark,
    "report": cmd_report,
    "inspect-sql": cmd_inspect_sql,
    "mirror": cmd_mirror,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns an integer exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except NotImplementedError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
