import argparse
import csv
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

DEFAULT_USD_IDR_RATE = 15500

PERIOD_END_DATES = {
    "Q1": (3, 31),
    "Q2": (6, 30),
    "Q3": (9, 30),
    "Q4": (12, 31),
}

_RATE_CACHE: dict = {}

try:
    from forex_engine import get_closing_rate
except ImportError:
    import yfinance as yf

    def get_closing_rate(year: int, period: str) -> float:
        cache_key = (year, period)
        if cache_key in _RATE_CACHE:
            return _RATE_CACHE[cache_key]

        month, day = PERIOD_END_DATES.get(period, (12, 31))
        target_date = datetime(year, month, day)
        start = (target_date - timedelta(days=10)).strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            history = yf.Ticker("USDIDR=X").history(start=start, end=end)
            if history.empty:
                raise ValueError("no price data returned for period")
            rate = float(history["Close"].iloc[-1])
        except Exception:
            logger.warning(
                "Failed to fetch USD/IDR rate for %s %s from Yahoo Finance, "
                "using fixed fallback rate of %s",
                year, period, DEFAULT_USD_IDR_RATE,
            )
            rate = DEFAULT_USD_IDR_RATE

        _RATE_CACHE[cache_key] = rate
        return rate

PERIODS = ["Q1", "Q2", "Q3"]

REVENUE_TAG_PATTERNS = [
    "SalesAndRevenue",
    "TotalInterestAndShariaIncome",
    "TotalOperatingIncome",
    "OperatingRevenue",
    "NetPremiumIncome",
    "PremiumIncome",
    "Revenue",
    "NetSales",
]

METRIC_FIELDS = [
    "total_assets", "current_assets", "total_liabilities", "current_liabilities",
    "total_equity", "revenue", "net_income", "operating_cash_flow",
    "outstanding_shares", "capital_expenditure",
]

OUTPUT_FIELDS = (
    ["ticker", "year", "period", "currency", "fx_rate_at_report"]
    + METRIC_FIELDS
    + ["revenue_tag_source"]
)

TRUE_VALUES = {"1", "true", "t", "yes", "y"}


def _read_csv(path: Path, required_columns: set) -> list:
    if not path.exists():
        raise FileNotFoundError(f"Required input file not found: {path}")

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing columns: {sorted(missing)}")
        return list(reader)


def load_emitens(path: Path) -> list:
    rows = _read_csv(path, {"ticker", "taxonomy_type"})
    emitens = []
    for row in rows:
        is_active = row.get("is_active")
        if is_active is not None and is_active.strip().lower() not in TRUE_VALUES:
            continue
        ticker = row["ticker"].strip().upper()
        if ticker:
            emitens.append((ticker, row["taxonomy_type"].strip()))
    return emitens


def load_taxonomy_maps(path: Path) -> dict:
    rows = _read_csv(path, {"taxonomy_type", "common_term", "xbrl_tag"})
    maps = {}
    for row in rows:
        term_map = maps.setdefault(row["taxonomy_type"].strip(), {})
        term_map.setdefault(row["common_term"].strip(), []).append(row["xbrl_tag"].strip())
    return maps


def load_reports(path: Path) -> dict:
    reports = {}
    if not path.exists():
        return reports

    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["ticker"], int(row["year"]), row["period"])
            reports[key] = row
    return reports


def save_reports(path: Path, reports: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".csv.tmp")

    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for key in sorted(reports):
            writer.writerow({field: reports[key].get(field, "") for field in OUTPUT_FIELDS})

    os.replace(tmp_path, path)


def build_context_map(soup: BeautifulSoup, year: int) -> dict:
    context_map = {}
    for ctx in soup.find_all("context"):
        ctx_id = ctx.get("id")
        if ctx.find("instant"):
            label = "Instant"
            date_text = ctx.find("instant").text
        elif ctx.find("endDate"):
            label = "Duration"
            date_text = ctx.find("endDate").text
        else:
            continue
        prefix = "Current" if date_text.startswith(str(year)) else "Prior"
        context_map[ctx_id] = f"{prefix} {label}"
    return context_map


def extract_numeric_facts(soup: BeautifulSoup, context_map: dict) -> dict:
    facts = {}
    for el in soup.find_all(True):
        if not el.has_attr("contextRef"):
            continue
        try:
            value = float(el.text.strip())
        except ValueError:
            continue
        tag_name = el.name.split(":")[-1]
        context_label = context_map.get(el.get("contextRef"), "Unknown")
        facts.setdefault(tag_name, []).append({"v": value, "c": context_label})
    return facts


def pick_value(entries: list) -> float:
    current = [e for e in entries if "Current" in e["c"]]
    return current[0]["v"] if current else entries[0]["v"]


def resolve_metric(candidate_tags: list, facts: dict):
    for tag in candidate_tags:
        clean_tag = tag.split(":")[-1]
        entries = facts.get(clean_tag, [])
        if entries:
            return pick_value(entries), clean_tag
    return None, None


def resolve_revenue_fallback(facts: dict):
    for pattern in REVENUE_TAG_PATTERNS:
        entries = facts.get(pattern, [])
        if entries:
            return pick_value(entries), f"heuristic:{pattern}"
    return None, "not_found"


def process_filing(ticker: str, year: int, period: str, tax_type: str,
                   taxonomy_maps: dict, xbrl_path: Path) -> dict:
    with open(xbrl_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "lxml-xml")

    context_map = build_context_map(soup, year)
    facts = extract_numeric_facts(soup, context_map)

    currency = "USD" if soup.find(string=lambda t: t and "iso4217:USD" in t) else "IDR"
    fx_rate = get_closing_rate(year, period) if currency == "USD" else 1

    metrics = {}
    revenue_tag_source = None

    for term, candidate_tags in taxonomy_maps.get(tax_type, {}).items():
        value, tag_source = resolve_metric(candidate_tags, facts)
        if value is not None:
            metrics[term] = value
        if term == "revenue":
            revenue_tag_source = tag_source

    if "revenue" not in metrics:
        value, tag_source = resolve_revenue_fallback(facts)
        if value is not None:
            metrics["revenue"] = value
        revenue_tag_source = tag_source

    if "revenue" not in metrics:
        logger.warning("%s [%s %s]: revenue not found (taxonomy_type=%s)", ticker, period, year, tax_type)

    row = {
        "ticker": ticker,
        "year": year,
        "period": period,
        "currency": currency,
        "fx_rate_at_report": fx_rate,
        "revenue_tag_source": revenue_tag_source or "",
    }
    for field in METRIC_FIELDS:
        row[field] = metrics.get(field, 0)
    return row


def process_period(year: int, period: str, emitens: list, taxonomy_maps: dict,
                   xbrl_dir: Path, reports: dict) -> None:
    logger.info("Processing year=%s period=%s", year, period)

    processed = 0
    skipped_no_file = 0
    revenue_not_found = 0
    errors = 0

    for ticker, tax_type in emitens:
        xbrl_path = xbrl_dir / str(year) / period / f"{ticker}_{year}_{period}.xbrl"

        if not xbrl_path.exists():
            skipped_no_file += 1
            continue

        try:
            row = process_filing(ticker, year, period, tax_type, taxonomy_maps, xbrl_path)
            reports[(ticker, year, period)] = row
            if row["revenue_tag_source"] == "not_found":
                revenue_not_found += 1
            processed += 1
        except Exception:
            logger.exception("Failed to process %s [%s %s]", ticker, period, year)
            errors += 1

    logger.info(
        "Finished year=%s period=%s: %d processed, %d no file, %d revenue not found, %d errors",
        year, period, processed, skipped_no_file, revenue_not_found, errors,
    )


def parse_args() -> argparse.Namespace:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Parse XBRL filings into a CSV of key metrics.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=current_year)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_dir = args.data_dir
    xbrl_dir = data_dir / "XBRL"
    output_path = data_dir / "financial_reports.csv"

    emitens = load_emitens(data_dir / "emitens.csv")
    taxonomy_maps = load_taxonomy_maps(data_dir / "taxonomy.csv") 
    reports = load_reports(output_path)

    logger.info(
        "Starting XBRL loader (%d-%d): %d active issuers, %d existing rows",
        args.start_year, args.end_year, len(emitens), len(reports),
    )

    for year in range(args.start_year, args.end_year + 1):
        for period in PERIODS:
            process_period(year, period, emitens, taxonomy_maps, xbrl_dir, reports)
            save_reports(output_path, reports)

    logger.info("Wrote %d rows to %s", len(reports), output_path)


if __name__ == "__main__":
    main()