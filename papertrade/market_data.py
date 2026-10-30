"""Real daily closing prices from Yahoo Finance's public chart endpoint, cached in SQLite.

Yahoo covers both stocks (AAPL) and crypto (BTC-USD), so one source serves every bot.
It is unofficial and free: fine for an educational prototype, not for production.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable

from .assets import Asset, AssetClass
from .http import fetch as http_fetch
from .prices import HistoricalPriceFeed

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={start}&period2={end}&interval=1d"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    close TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);
"""


def yahoo_ticker(asset: Asset) -> str:
    return f"{asset.symbol}-USD" if asset.asset_class is AssetClass.CRYPTO else asset.symbol


def parse_chart(payload: bytes) -> list[tuple[date, Decimal]]:
    """Turn a Yahoo chart response into (day, close) pairs, skipping empty bars."""
    data = json.loads(payload)
    if data["chart"].get("error"):
        raise ValueError(data["chart"]["error"].get("description", "Yahoo chart error"))
    result = data["chart"]["result"][0]
    offset = result.get("meta", {}).get("gmtoffset", 0)
    closes = result["indicators"]["quote"][0]["close"]
    out = []
    for ts, close in zip(result.get("timestamp", []), closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()
        out.append((day, Decimal(f"{close:.2f}") if close >= 1 else Decimal(f"{close:.6f}")))
    return out


class PriceCache:
    def __init__(self, path=":memory:", fetch: Callable[[str], bytes] = http_fetch):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.fetch = fetch

    def update(self, assets: Iterable[Asset], start: date, end: date) -> dict[str, str]:
        """Download closes for [start, end] into the cache. Returns {symbol: error} for any that failed."""
        errors = {}
        to_ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        for asset in assets:
            url = CHART_URL.format(ticker=yahoo_ticker(asset), start=to_ts(start),
                                   end=to_ts(end + timedelta(days=1)))
            try:
                rows = parse_chart(self.fetch(url))
            except Exception as e:  # network down, bad symbol, rate limited
                errors[asset.symbol] = str(e)
                continue
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)",
                                    [(asset.symbol, d.isoformat(), str(c)) for d, c in rows])
        return errors

    def feed(self) -> HistoricalPriceFeed:
        rows = self.db.execute("SELECT day, symbol, close FROM prices")
        return HistoricalPriceFeed((date.fromisoformat(d), s, c) for d, s, c in rows)
