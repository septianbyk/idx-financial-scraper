import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[0] / "data"

MASTER_SCRIPT = "model/master-data.py"
FETCH_SCRIPT = "model/fetch_idx.py"
PARSE_SCRIPT = "model/arelle_loader.py"

PERIOD_CHOICES = ["Q1", "Q2", "Q3", "FY"]


def run_step(name: str, script: str, args: list) -> None:
    command = [sys.executable, str(SCRIPT_DIR / script), *args]
    logger.info("Step started: %s", name)
    started = time.time()

    result = subprocess.run(command)
    elapsed = time.time() - started

    if result.returncode != 0:
        raise RuntimeError(f"Step '{name}' failed with exit code {result.returncode}")

    logger.info("Step finished: %s (%.1f seconds)", name, elapsed)


def validate_inputs(args: argparse.Namespace, watchlist_path: Path) -> None:
    needs_watchlist = not args.skip_master or not args.skip_fetch
    if needs_watchlist and not watchlist_path.exists():
        raise FileNotFoundError(f"Watchlist not found: {watchlist_path}")

    if not args.skip_parse:
        mappings_path = args.data_dir / "taxonomy.csv"
        if not mappings_path.exists():
            raise FileNotFoundError(f"Taxonomy mappings not found: {mappings_path}")

    if args.skip_master and not args.skip_parse:
        emitens_path = args.data_dir / "emitens.csv"
        if not emitens_path.exists():
            raise FileNotFoundError(
                f"Issuer list not found: {emitens_path}. Run the master data step first."
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full pipeline: master data, IDX download, XBRL parsing."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--start-year", type=int, default=None,
                        help="First year for download and parsing")
    parser.add_argument("--end-year", type=int, default=None,
                        help="Last year for download and parsing")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Optional subset of tickers for the download step")
    parser.add_argument("--periods", nargs="+", choices=PERIOD_CHOICES, default=None,
                        help="Optional subset of periods for the download step")
    parser.add_argument("--pdf", action="store_true",
                        help="Only download PDF financial statements into <data-dir>/FinancialStatement")
    parser.add_argument("--skip-master", action="store_true", help="Skip the master data step")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip the IDX download step")
    parser.add_argument("--skip-parse", action="store_true", help="Skip the XBRL parsing step")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.pdf:
        args.skip_master = True
        args.skip_parse = True
        args.skip_fetch = False
        logger.info("PDF mode: master data and XBRL parsing are skipped")

    watchlist_path = args.watchlist or (args.data_dir / "watchlist.csv")

    try:
        validate_inputs(args, watchlist_path)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 1

    base_args = ["--data-dir", str(args.data_dir)]
    watchlist_args = ["--watchlist", str(watchlist_path)]

    year_args = []
    if args.start_year is not None:
        year_args += ["--start-year", str(args.start_year)]
    if args.end_year is not None:
        year_args += ["--end-year", str(args.end_year)]

    fetch_extra_args = []
    if args.tickers:
        fetch_extra_args += ["--tickers", *args.tickers]
    if args.periods:
        fetch_extra_args += ["--periods", *args.periods]
    if args.pdf:
        fetch_extra_args.append("--pdf")

    steps = []
    if not args.skip_master:
        steps.append(("Master data", MASTER_SCRIPT, base_args + watchlist_args))
    if not args.skip_fetch:
        fetch_name = "IDX PDF download" if args.pdf else "IDX download"
        steps.append((fetch_name, FETCH_SCRIPT, base_args + watchlist_args + year_args + fetch_extra_args))
    if not args.skip_parse:
        steps.append(("XBRL parsing", PARSE_SCRIPT, base_args + year_args))

    if not steps:
        logger.warning("All steps were skipped, nothing to do")
        return 0

    started = time.time()
    try:
        for name, script, step_args in steps:
            run_step(name, script, step_args)
    except RuntimeError as e:
        logger.error("%s", e)
        logger.error("Pipeline stopped")
        return 1
    except KeyboardInterrupt:
        logger.error("Pipeline interrupted")
        return 130

    logger.info("Pipeline finished in %.1f seconds", time.time() - started)
    if args.pdf:
        logger.info("Output: %s", args.data_dir / "FinancialStatement")
    else:
        logger.info("Output: %s", args.data_dir / "financial_reports.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
