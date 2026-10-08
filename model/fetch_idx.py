import argparse
import base64
import csv
import io
import logging
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

IDX_HOME = "https://www.idx.co.id/id"

PERIODS = [
    ("TW1", "Q1"),
    ("TW2", "Q2"),
    ("TW3", "Q3"),
    ("Audit", "FY"),
]
PERIOD_TAGS = [tag for _, tag in PERIODS]

PDF_PERIOD_TOKEN = {
    "Q1": "I",
    "Q2": "II",
    "Q3": "III",
    "FY": "Tahunan",
}

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)

PDF_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/"
    "FinancialStatement-{year}-{token}-{ticker}.pdf"
)

CHROME_CANDIDATES = [
    os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
    os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
]

DEFAULT_DEBUG_PORT = 9222
CHROME_START_TIMEOUT_SECONDS = 30

REQUEST_DELAY_SECONDS = 2.0
PERIOD_DELAY_SECONDS = 10
PROGRESS_EVERY = 25
MAX_CHALLENGE_RETRIES = 2

FETCH_JS = """
async (url) => {
    const response = await fetch(url, { credentials: 'include' });
    const result = { status: response.status, b64: null };
    if (response.status === 200) {
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        result.b64 = btoa(binary);
    }
    return result;
}
"""


class BlockedError(RuntimeError):
    pass


def debug_port_is_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False


def find_chrome(explicit_path):
    if explicit_path:
        path = Path(explicit_path)
        if not path.exists():
            raise FileNotFoundError(f"Chrome executable not found: {path}")
        return str(path)
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Chrome not found in the standard locations. Pass --chrome-path with the full path to chrome.exe."
    )


def ensure_chrome_running(port: int, profile_dir: Path, chrome_path) -> None:
    if debug_port_is_open(port):
        logger.info("Chrome already listening on debug port %d, attaching", port)
        return

    exe = find_chrome(chrome_path)
    profile_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting Chrome with profile %s", profile_dir)
    subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            IDX_HOME,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + CHROME_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        if debug_port_is_open(port):
            return
        time.sleep(0.5)

    raise RuntimeError(
        f"Chrome did not open debug port {port} within {CHROME_START_TIMEOUT_SECONDS}s. "
        "Close all Chrome windows that use the same profile folder and try again."
    )


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


def extract_xbrl(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = z.namelist()
        xbrl_name = next((n for n in names if n.lower().endswith(".xbrl")), None)
        if xbrl_name is None:
            xbrl_name = next((n for n in names if n.lower().endswith(".xml")), None)
        return z.read(xbrl_name) if xbrl_name else None


def open_idx_home(page) -> None:
    try:
        page.goto(IDX_HOME, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        logger.warning("Navigation to IDX home did not finish cleanly: %s", e)


def page_is_challenge(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return "just a moment" in title or "attention required" in title


def wait_for_manual_challenge(page) -> None:
    if not sys.stdin or not sys.stdin.isatty():
        raise BlockedError(
            "Cloudflare challenge detected and no interactive terminal is available. "
            "Run this script directly from a terminal once to clear the challenge."
        )

    open_idx_home(page)
    print()
    print("Cloudflare verification may be required.")
    print("In the Chrome window, make sure the IDX website loads normally.")
    answer = input("Press Enter to continue, or type q to quit: ").strip().lower()
    if answer == "q":
        raise BlockedError("Stopped by user at the verification prompt.")


def fetch_in_browser(page, url: str):
    result = page.evaluate(FETCH_JS, url)
    status = result["status"]
    content = base64.b64decode(result["b64"]) if result.get("b64") else None
    return status, content


def bulk_download(
    page,
    tickers: list,
    xbrl_dir: Path,
    failed_log_path: Path,
    year: int,
    period_folder: str,
    period_tag: str,
) -> None:
    logger.info("Starting download: year=%s period=%s tickers=%d", year, period_tag, len(tickers))

    target_dir = xbrl_dir / str(year) / period_tag
    counts = {"saved": 0, "skipped_existing": 0, "not_found": 0, "blocked": 0, "http_error": 0, "errors": 0}

    for index, ticker in enumerate(tickers, start=1):
        target_file = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"

        if target_file.exists():
            counts["skipped_existing"] += 1
            continue

        url = IDX_URL_TEMPLATE.format(year=year, period_folder=period_folder, ticker=ticker)
        challenge_attempts = 0

        while True:
            try:
                status, content = fetch_in_browser(page, url)
            except Exception as e:
                logger.error("Failed to download %s: %s", ticker, e)
                log_failed_download(failed_log_path, ticker, year, period_tag, str(e))
                counts["errors"] += 1
                break

            is_zip = content is not None and content[:2] == b"PK"

            if status == 200 and is_zip:
                try:
                    xbrl_bytes = extract_xbrl(content)
                except zipfile.BadZipFile as e:
                    logger.error("%s: invalid zip archive: %s", ticker, e)
                    log_failed_download(failed_log_path, ticker, year, period_tag, "bad_zip")
                    counts["errors"] += 1
                    break

                if xbrl_bytes is None:
                    logger.warning("%s: zip archive contained no XBRL file", ticker)
                    log_failed_download(failed_log_path, ticker, year, period_tag, "no_xbrl_in_zip")
                    counts["errors"] += 1
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(xbrl_bytes)
                    logger.info("Saved %s [%s %s]", ticker, period_tag, year)
                    counts["saved"] += 1
                break

            if status == 404:
                counts["not_found"] += 1
                break

            if status in (403, 429) or status == 200:
                if challenge_attempts >= MAX_CHALLENGE_RETRIES:
                    logger.warning("%s: still blocked after %d verification attempts", ticker, challenge_attempts)
                    log_failed_download(failed_log_path, ticker, year, period_tag, f"blocked_http_{status}")
                    counts["blocked"] += 1
                    raise BlockedError(f"Repeated blocking at {ticker} [{period_tag} {year}].")
                logger.warning("%s: blocked (HTTP %s)", ticker, status)
                wait_for_manual_challenge(page)
                challenge_attempts += 1
                continue

            logger.warning("%s: unexpected HTTP %s", ticker, status)
            log_failed_download(failed_log_path, ticker, year, period_tag, f"http_{status}")
            counts["http_error"] += 1
            break

        if index % PROGRESS_EVERY == 0:
            logger.info("Progress %s %s: %d/%d tickers", period_tag, year, index, len(tickers))

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Finished year=%s period=%s: %d saved, %d already on disk, %d not found (404), "
        "%d blocked, %d other HTTP errors, %d errors",
        year, period_tag, counts["saved"], counts["skipped_existing"], counts["not_found"],
        counts["blocked"], counts["http_error"], counts["errors"],
    )


def bulk_download_pdf(
    page,
    tickers: list,
    pdf_dir: Path,
    failed_log_path: Path,
    year: int,
    period_folder: str,
    period_tag: str,
) -> None:
    logger.info("Starting PDF download: year=%s period=%s tickers=%d", year, period_tag, len(tickers))

    token = PDF_PERIOD_TOKEN[period_tag]
    target_dir = pdf_dir / str(year) / period_tag
    counts = {"saved": 0, "skipped_existing": 0, "not_found": 0, "blocked": 0, "http_error": 0, "errors": 0}

    for index, ticker in enumerate(tickers, start=1):
        target_file = target_dir / f"FinancialStatement-{year}-{token}-{ticker}.pdf"

        if target_file.exists():
            counts["skipped_existing"] += 1
            continue

        url = PDF_URL_TEMPLATE.format(year=year, period_folder=period_folder, ticker=ticker, token=token)
        challenge_attempts = 0

        while True:
            try:
                status, content = fetch_in_browser(page, url)
            except Exception as e:
                logger.error("Failed to download PDF %s: %s", ticker, e)
                log_failed_download(failed_log_path, ticker, year, period_tag, f"pdf_{e}")
                counts["errors"] += 1
                break

            is_pdf = content is not None and content[:4] == b"%PDF"

            if status == 200 and is_pdf:
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(content)
                logger.info("Saved PDF %s [%s %s]", ticker, period_tag, year)
                counts["saved"] += 1
                break

            if status == 404:
                logger.info("%s: PDF not found [%s %s]", ticker, period_tag, year)
                counts["not_found"] += 1
                break

            if status in (403, 429) or status == 200:
                if challenge_attempts >= MAX_CHALLENGE_RETRIES:
                    logger.warning("%s: still blocked after %d verification attempts", ticker, challenge_attempts)
                    log_failed_download(failed_log_path, ticker, year, period_tag, f"pdf_blocked_http_{status}")
                    counts["blocked"] += 1
                    raise BlockedError(f"Repeated blocking at {ticker} [{period_tag} {year}].")
                logger.warning("%s: blocked (HTTP %s)", ticker, status)
                wait_for_manual_challenge(page)
                challenge_attempts += 1
                continue

            logger.warning("%s: unexpected HTTP %s", ticker, status)
            log_failed_download(failed_log_path, ticker, year, period_tag, f"pdf_http_{status}")
            counts["http_error"] += 1
            break

        if index % PROGRESS_EVERY == 0:
            logger.info("Progress PDF %s %s: %d/%d tickers", period_tag, year, index, len(tickers))

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Finished PDF year=%s period=%s: %d saved, %d already on disk, %d not found (404), "
        "%d blocked, %d other HTTP errors, %d errors",
        year, period_tag, counts["saved"], counts["skipped_existing"], counts["not_found"],
        counts["blocked"], counts["http_error"], counts["errors"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download IDX XBRL filings or PDF financial statements for a watchlist.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--start-year", type=int, default=2022)
    parser.add_argument("--end-year", type=int, default=2022)
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Optional subset of tickers, e.g. --tickers AALI ADRO")
    parser.add_argument("--periods", nargs="+", choices=PERIOD_TAGS, default=None,
                        help="Optional subset of periods, e.g. --periods Q1 FY")
    parser.add_argument("--pdf", action="store_true",
                        help="Download the PDF financial statement into <data-dir>/FinancialStatement instead of XBRL")
    parser.add_argument("--port", type=int, default=DEFAULT_DEBUG_PORT,
                        help="Chrome remote debugging port (default: %(default)s)")
    parser.add_argument("--chrome-path", type=str, default=None,
                        help="Full path to chrome.exe if it is not in a standard location")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_dir = args.data_dir
    watchlist_path = args.watchlist or (data_dir / "watchlist.csv")
    xbrl_dir = data_dir / "XBRL"
    pdf_dir = data_dir / "FinancialStatement"
    failed_log_path = data_dir / "failed_downloads.csv"
    profile_dir = data_dir / "chrome_profile"

    tickers = load_watchlist(watchlist_path)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers}
        tickers = [t for t in tickers if t in wanted]
    if not tickers:
        logger.warning("Watchlist is empty, nothing to download.")
        return

    periods = [p for p in PERIODS if args.periods is None or p[1] in args.periods]

    logger.info("Loaded %d tickers from %s", len(tickers), watchlist_path)

    ensure_chrome_running(args.port, profile_dir, args.chrome_path)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if "idx.co.id" not in page.url or page_is_challenge(page):
                open_idx_home(page)

            if page_is_challenge(page):
                wait_for_manual_challenge(page)

            for year in range(args.start_year, args.end_year + 1):
                for period_folder, period_tag in periods:
                    if args.pdf:
                        bulk_download_pdf(
                            page, tickers, pdf_dir, failed_log_path,
                            year, period_folder, period_tag,
                        )
                    else:
                        bulk_download(
                            page, tickers, xbrl_dir, failed_log_path,
                            year, period_folder, period_tag,
                        )
                    logger.info("Pausing before next period")
                    time.sleep(PERIOD_DELAY_SECONDS)

            logger.info("All periods from %s to %s processed", args.start_year, args.end_year)
        except BlockedError as e:
            logger.error("%s Re-run later; existing files are skipped automatically.", e)
        except KeyboardInterrupt:
            logger.error("Interrupted by user; existing files are skipped on the next run.")
        finally:
            browser.close()

    if failed_log_path.exists():
        logger.info("Some downloads failed; see %s", failed_log_path)


if __name__ == "__main__":
    main()
