import argparse
import csv
import io
import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path

import cloudscraper
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

PERIODS = [
    ("TW1", "Q1"),
    ("TW2", "Q2"),
    ("TW3", "Q3"),
]

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [5, 15, 45]
REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 1.5
PERIOD_DELAY_SECONDS = 10


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


def log_failed_download(log_path: Path, ticker: str, year: int, period_tag: str, reason: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not log_path.exists()
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["ticker", "year", "period", "reason", "logged_at"])
        writer.writerow([ticker, year, period_tag, reason, datetime.now().isoformat()])


def download_with_retry(scraper: cloudscraper.CloudScraper, url: str, ticker: str):
    last_exception = None

    for attempt in range(MAX_RETRIES):
        try:
            return scraper.get(url, timeout=REQUEST_TIMEOUT)
        except (
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
        ) as e:
            last_exception = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Timeout for %s (attempt %d/%d), retrying in %ds",
                    ticker, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)

    raise last_exception


def bulk_download(
    tickers: list,
    xbrl_dir: Path,
    failed_log_path: Path,
    year: int,
    period_folder: str,
    period_tag: str,
) -> None:
    logger.info("Starting download: year=%s period=%s", year, period_tag)

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    saved = 0
    missing = 0
    errors = 0

    for ticker in tickers:
        url = IDX_URL_TEMPLATE.format(year=year, period_folder=period_folder, ticker=ticker)

        try:
            response = download_with_retry(scraper, url, ticker)

            if response.status_code != 200:
                missing += 1
            else:
                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    xbrl_name = next(
                        (n for n in z.namelist() if n.endswith(".xbrl") or n.endswith(".xml")),
                        None,
                    )

                    if xbrl_name is None:
                        logger.warning("%s: zip archive contained no XBRL file", ticker)
                        missing += 1
                    else:
                        target_dir = xbrl_dir / str(year) / period_tag
                        target_dir.mkdir(parents=True, exist_ok=True)

                        target_file = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"
                        target_file.write_bytes(z.read(xbrl_name))

                        logger.info("Saved %s [%s %s]", ticker, period_tag, year)
                        saved += 1

        except Exception as e:
            logger.error("Failed to download or extract %s: %s", ticker, e)
            log_failed_download(failed_log_path, ticker, year, period_tag, str(e))
            errors += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Finished year=%s period=%s: %d saved, %d missing or not yet released, %d errors",
        year, period_tag, saved, missing, errors,
    )


def parse_args() -> argparse.Namespace:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Download IDX XBRL filings for a watchlist.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=current_year)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_dir = args.data_dir
    watchlist_path = args.watchlist or (data_dir / "watchlist.csv")
    xbrl_dir = data_dir / "XBRL"
    failed_log_path = data_dir / "failed_downloads.csv"

    tickers = load_watchlist(watchlist_path)
    if not tickers:
        logger.warning("Watchlist is empty, nothing to download.")
        return

    logger.info("Loaded %d tickers from %s", len(tickers), watchlist_path)

    for year in range(args.start_year, args.end_year + 1):
        for period_folder, period_tag in PERIODS:
            bulk_download(tickers, xbrl_dir, failed_log_path, year, period_folder, period_tag)
            logger.info("Pausing before next period")
            time.sleep(PERIOD_DELAY_SECONDS)

    logger.info("All periods from %s to %s processed", args.start_year, args.end_year)
    if failed_log_path.exists():
        logger.info("Some downloads failed after retries; see %s for the retry list", failed_log_path)


if __name__ == "__main__":
    main()