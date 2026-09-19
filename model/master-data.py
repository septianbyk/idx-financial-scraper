import argparse
import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

REQUEST_DELAY_SECONDS = 1.0
DEFAULT_SHARES_START_YEAR = 2019

EMITEN_FIELDS = [
    "ticker", "name", "sector", "industry",
    "taxonomy_type", "is_active", "created_at",
]

REPORT_FIELDS = [
    "ticker", "year", "period", "currency", "fx_rate_at_report",
    "total_assets", "current_assets", "total_liabilities", "current_liabilities",
    "total_equity", "revenue", "net_income", "operating_cash_flow",
    "outstanding_shares", "capital_expenditure", "revenue_tag_source",
]


def classify_taxonomy(sector: str, industry: str) -> str:
    if "insurance" in industry.lower():
        return "insurance"
    if "Financial" in sector:
        return "banking"
    return "general"


def load_watchlist(path: Path) -> list:
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found: {path}. Create a CSV file with a 'ticker' header."
        )

    tickers = []
    seen = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError(f"{path} must contain a 'ticker' column")
        for row in reader:
            ticker = (row["ticker"] or "").strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
    return tickers


def load_emitens(path: Path) -> dict:
    emitens = {}
    if not path.exists():
        return emitens

    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            emitens[row["ticker"]] = row
    return emitens


def save_emitens(path: Path, emitens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".csv.tmp")

    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EMITEN_FIELDS)
        writer.writeheader()
        for ticker in sorted(emitens):
            writer.writerow({field: emitens[ticker].get(field, "") for field in EMITEN_FIELDS})

    os.replace(tmp_path, path)


def load_reports(path: Path) -> dict:
    reports = {}
    if not path.exists():
        return reports

    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            reports[(row["ticker"], int(row["year"]), row["period"])] = row
    return reports


def save_reports(path: Path, reports: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".csv.tmp")

    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for key in sorted(reports):
            writer.writerow({field: reports[key].get(field, "") for field in REPORT_FIELDS})

    os.replace(tmp_path, path)


def upsert_emiten(emitens: dict, ticker: str, name: str, sector: str,
                  industry: str, taxonomy_type: str) -> None:
    existing = emitens.get(ticker)
    emitens[ticker] = {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "industry": industry,
        "taxonomy_type": taxonomy_type,
        "is_active": existing["is_active"] if existing else "true",
        "created_at": existing["created_at"] if existing else datetime.now().isoformat(timespec="seconds"),
    }


def upsert_shares(reports: dict, ticker: str, year: int, shares: int) -> None:
    key = (ticker, year, "FY")
    if key in reports:
        reports[key]["outstanding_shares"] = shares
    else:
        reports[key] = {
            "ticker": ticker,
            "year": year,
            "period": "FY",
            "outstanding_shares": shares,
        }


def sync_master_data(tickers: list, emitens: dict, reports: dict, start_year: int) -> None:
    current_year = datetime.now().year
    synced = 0
    no_shares_data = 0
    errors = 0

    for ticker in tickers:
        try:
            info = yf.Ticker(f"{ticker}.JK").info

            sector = info.get("sector") or "Unknown"
            industry = info.get("industry") or "Unknown"
            name = info.get("longName") or ticker
            shares = info.get("sharesOutstanding") or 0

            if sector == "Unknown":
                logger.warning("%s: no sector data from yfinance, defaulting to 'general'", ticker)

            taxonomy_type = classify_taxonomy(sector, industry)
            upsert_emiten(emitens, ticker, name, sector, industry, taxonomy_type)

            if shares > 0:
                for year in range(start_year, current_year + 1):
                    upsert_shares(reports, ticker, year, shares)
                logger.info(
                    "%s synced: taxonomy=%s shares=%s (applied to FY %d-%d as placeholder)",
                    ticker, taxonomy_type, f"{shares:,}", start_year, current_year,
                )
                synced += 1
            else:
                logger.warning(
                    "%s synced: taxonomy=%s, no shares outstanding data from yfinance",
                    ticker, taxonomy_type,
                )
                no_shares_data += 1

        except Exception:
            logger.exception("Failed to process %s", ticker)
            errors += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Sync complete: %d synced, %d missing shares data, %d errors",
        synced, no_shares_data, errors,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build emitens.csv from a watchlist using yfinance.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--shares-start-year", type=int, default=DEFAULT_SHARES_START_YEAR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_dir = args.data_dir
    watchlist_path = args.watchlist or (data_dir / "watchlist.csv")
    emitens_path = data_dir / "emitens.csv"
    reports_path = data_dir / "financial_reports.csv"

    tickers = load_watchlist(watchlist_path)
    if not tickers:
        logger.warning("Watchlist is empty, nothing to sync.")
        return

    logger.info("Found %d tickers in %s", len(tickers), watchlist_path)

    emitens = load_emitens(emitens_path)
    reports = load_reports(reports_path)

    try:
        sync_master_data(tickers, emitens, reports, args.shares_start_year)
    finally:
        save_emitens(emitens_path, emitens)
        save_reports(reports_path, reports)
        logger.info("Wrote %d issuers to %s", len(emitens), emitens_path)
        logger.info("Wrote %d rows to %s", len(reports), reports_path)


if __name__ == "__main__":
    main()