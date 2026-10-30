"""Long price histories for research: imported from Kaggle-style CSV files or downloaded from Yahoo.

Supported CSV shapes (column names are matched loosely, case doesn't matter):
  * one file per symbol: Date, Open, High, Low, Close, [Adj Close], Volume
    (symbol from the file name: AAPL.csv, aapl.us.txt, BTC-USD.csv, coin_Bitcoin.csv)
  * many symbols in one file, with a Name, Symbol or Ticker column (e.g. all_stocks_5yr.csv)
Adjusted close is preferred when present, so splits and dividends don't look like crashes.
"""
import csv
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable

from .assets import Asset
from .http import fetch as http_fetch
from .market_data import CHART_URL, yahoo_ticker
from .prices import HistoricalPriceFeed

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    close TEXT NOT NULL,
    volume REAL,               -- NULL when the source had none
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);
"""

COIN_NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "dogecoin": "DOGE"}
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


def normalize_symbol(raw: str) -> str:
    s = raw.strip()
    if s.lower().startswith("coin_"):
        s = s[5:]
    if s.lower() in COIN_NAMES:
        return COIN_NAMES[s.lower()]
    s = s.upper()
    s = re.sub(r"\.(US|TXT|CSV)$", "", s)
    s = re.sub(r"[-/](USD|USDT)$", "", s)
    return s


def parse_day(text: str) -> date:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text[:19] if " " in text else text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError(f"unrecognised date {text!r}")


def _pick(header: list[str], *names: str) -> str | None:
    lower = {h.strip().lower().replace("_", " "): h for h in header}
    for n in names:
        if n in lower:
            return lower[n]
    return None


def read_csv(path: Path) -> tuple[list[tuple[str, date, Decimal, float | None]], list[str]]:
    """Rows of (symbol, day, close, volume) from one CSV file, plus warnings about skipped rows."""
    rows, warnings = [], []
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_date = _pick(header, "date", "datetime", "timestamp", "time")
        c_close = _pick(header, "adj close", "adjclose", "adjusted close", "close", "price")
        c_vol = _pick(header, "volume", "vol")
        c_sym = _pick(header, "symbol", "ticker", "name")
        if not (c_date and c_close):
            return [], [f"{path.name}: no date/close columns in {header}"]
        file_symbol = normalize_symbol(path.stem)
        for n, r in enumerate(reader, 2):
            try:
                close = Decimal(r[c_close].replace(",", ""))
                if close <= 0:
                    continue
                vol = float(r[c_vol].replace(",", "")) if c_vol and r.get(c_vol) not in (None, "", "null") else None
                symbol = normalize_symbol(r[c_sym]) if c_sym and r.get(c_sym) else file_symbol
                rows.append((symbol, parse_day(r[c_date]), close, vol))
            except (InvalidOperation, ValueError, TypeError, AttributeError) as e:
                if len(warnings) < 5:
                    warnings.append(f"{path.name} line {n}: skipped ({e})")
    return rows, warnings


class HistoryStore:
    def __init__(self, path="history.db", fetch: Callable[[str], bytes] = http_fetch):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.fetch = fetch

    def add(self, rows: Iterable[tuple[str, date, Decimal, float | None]], source: str) -> int:
        before = self.db.total_changes
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?)",
                                [(s, d.isoformat(), str(c), v, source) for s, d, c, v in rows])
        return self.db.total_changes - before

    def import_csv(self, path, only: set[str] | None = None) -> tuple[dict[str, int], list[str]]:
        """Import one CSV file or every .csv/.txt file in a folder. Returns ({symbol: rows}, warnings)."""
        path = Path(path)
        files = sorted(p for p in path.rglob("*") if p.suffix.lower() in (".csv", ".txt")) if path.is_dir() else [path]
        counts, warnings = {}, []
        for f in files:
            rows, w = read_csv(f)
            warnings += w
            if only:
                rows = [r for r in rows if r[0] in only]
            if rows:
                self.add(rows, f"csv:{f.name}")
                for r in rows:
                    counts[r[0]] = counts.get(r[0], 0) + 1
        return counts, warnings

    def import_yahoo(self, assets: Iterable[Asset], years: int) -> dict[str, str]:
        """Download daily history from Yahoo (adjusted close and volume). Returns {symbol: error}."""
        end = date.today()
        start = end - timedelta(days=365 * years)
        ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        errors = {}
        for a in assets:
            try:
                data = json.loads(self.fetch(CHART_URL.format(ticker=yahoo_ticker(a), start=ts(start), end=ts(end))
                                             + "&events=div,splits"))
                res = data["chart"]["result"][0]
                offset = res.get("meta", {}).get("gmtoffset", 0)
                adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
                closes = adj or res["indicators"]["quote"][0]["close"]
                vols = res["indicators"]["quote"][0].get("volume") or [None] * len(closes)
                rows = [(a.symbol, datetime.fromtimestamp(t + offset, tz=timezone.utc).date(),
                         Decimal(f"{c:.6f}"), v) for t, c, v in zip(res["timestamp"], closes, vols) if c]
                self.add(rows, "yahoo")
            except Exception as e:
                errors[a.symbol] = str(e)
        return errors

    def symbols(self) -> list[tuple[str, str, str, int]]:
        """(symbol, first day, last day, rows) for everything stored."""
        return list(self.db.execute(
            "SELECT symbol, MIN(day), MAX(day), COUNT(*) FROM bars GROUP BY symbol ORDER BY symbol"))

    def series(self, symbol: str, start: date | None = None, end: date | None = None
               ) -> list[tuple[date, float, float | None]]:
        sql, args = "SELECT day, close, volume FROM bars WHERE symbol = ?", [symbol]
        if start:
            sql, args = sql + " AND day >= ?", args + [start.isoformat()]
        if end:
            sql, args = sql + " AND day <= ?", args + [end.isoformat()]
        return [(date.fromisoformat(d), float(c), v) for d, c, v in self.db.execute(sql + " ORDER BY day", args)]

    def feed(self) -> HistoricalPriceFeed:
        return HistoricalPriceFeed((date.fromisoformat(d), s, c)
                                   for s, d, c in self.db.execute("SELECT symbol, day, close FROM bars"))
