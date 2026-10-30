"""Headlines from free RSS feeds (Yahoo Finance per ticker, Google News search), cached in SQLite.

RSS only carries recent stories, so news is collected going forward: run the fetch daily
and the cache builds up a history the bots can be replayed against later.
"""
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Callable, Iterable
from urllib.parse import quote_plus
import re

from .assets import Asset, AssetClass
from .http import fetch as http_fetch

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
GOOGLE_RSS = "https://news.google.com/rss/search?q={query}+when:2d&hl=en-US&gl=US&ceid=US:en"
MARKET_QUERIES = ["stock market", "federal reserve interest rates", "cryptocurrency market"]
MARKET = "*"  # symbol tag for market-wide stories

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    link TEXT PRIMARY KEY,
    published TEXT NOT NULL,
    symbol TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS news_by_symbol ON news (symbol, published);
"""


@dataclass(frozen=True)
class NewsItem:
    published: datetime
    symbol: str
    title: str
    source: str
    link: str
    summary: str = ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# Yahoo's ticker feeds mix in option-quote pages ("GME Nov 2026 25.000 call (GME261120C00025000)").
OPTION_QUOTE = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b|\b\d+\.\d{3} (call|put)\b", re.I)


def parse_rss(payload: bytes, symbol: str, default_source: str) -> list[NewsItem]:
    items = []
    for item in ET.fromstring(payload).iter("item"):
        title, link, pub = item.findtext("title"), item.findtext("link"), item.findtext("pubDate")
        if not (title and link and pub) or OPTION_QUOTE.search(title):
            continue
        published = parsedate_to_datetime(pub)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        source = item.findtext("source") or default_source
        items.append(NewsItem(published.astimezone(timezone.utc), symbol, _clean(title), _clean(source),
                              link.strip(), _clean(item.findtext("description"))[:400]))
    return items


# Google searches by name: a bare ticker like "KO" matches unrelated stories (Korean news, "KO'd").
NAMES = {"AAPL": '"Apple" stock', "MSFT": '"Microsoft" stock', "NVDA": '"Nvidia" stock',
         "KO": '"Coca-Cola" stock', "JNJ": '"Johnson & Johnson" stock', "GME": '"GameStop" stock',
         "SPY": '"S&P 500"', "BTC": "bitcoin price", "ETH": "ethereum price", "SOL": "solana price",
         "DOGE": "dogecoin price"}


def news_urls(asset: Asset) -> list[tuple[str, str]]:
    """(url, source label) pairs for one asset."""
    crypto = asset.asset_class is AssetClass.CRYPTO
    query = NAMES.get(asset.symbol, f"{asset.symbol} {'price' if crypto else 'stock'}")
    ticker = f"{asset.symbol}-USD" if crypto else asset.symbol
    return [(YAHOO_RSS.format(ticker=ticker), "Yahoo Finance"),
            (GOOGLE_RSS.format(query=quote_plus(query)), "Google News")]


class NewsCache:
    def __init__(self, path=":memory:", fetch: Callable[[str], bytes] = http_fetch):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.fetch = fetch

    def add(self, items: Iterable[NewsItem]) -> int:
        before = self.db.total_changes
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?)",
                                [(i.link, i.published.isoformat(), i.symbol, i.title, i.source, i.summary)
                                 for i in items])
        return self.db.total_changes - before

    def update(self, assets: Iterable[Asset]) -> dict[str, str]:
        """Pull the latest headlines for each asset plus market-wide queries. Returns {url: error}."""
        jobs = [(url, label, a.symbol) for a in assets for url, label in news_urls(a)]
        jobs += [(GOOGLE_RSS.format(query=quote_plus(q)), "Google News", MARKET) for q in MARKET_QUERIES]
        errors = {}
        for url, label, symbol in jobs:
            try:
                self.add(parse_rss(self.fetch(url), symbol, label))
            except Exception as e:
                errors[url] = str(e)
        return errors

    def update_topics(self, topics: dict[str, list[str]]) -> dict[str, str]:
        """Pull Google News searches for research-desk topics, tagged e.g. 'topic:energy'."""
        errors = {}
        for tag, queries in topics.items():
            for q in queries:
                url = GOOGLE_RSS.format(query=quote_plus(q))
                try:
                    self.add(parse_rss(self.fetch(url), tag, "Google News"))
                except Exception as e:
                    errors[url] = str(e)
        return errors

    def headlines(self, day: date, symbols: Iterable[str], lookback_days: int = 1,
                  per_symbol: int = 3, include_market: bool = True) -> list[NewsItem]:
        """Stories published before the end of `day` (UTC) and within the lookback window.

        Nothing from after `day` is ever returned, so a replayed bot can't peek at the future.
        """
        end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
        start = end - timedelta(days=lookback_days + 1)
        out, seen = [], set()
        for symbol in list(symbols) + ([MARKET] if include_market else []):
            rows = self.db.execute(
                "SELECT published, symbol, title, source, link, summary FROM news"
                " WHERE symbol = ? AND published >= ? AND published < ? ORDER BY published DESC",
                (symbol, start.isoformat(), end.isoformat()))
            picked = 0
            for p, s, t, src, link, summ in rows:
                key = t.lower()[:60]  # the same story often appears in both feeds
                if key in seen:
                    continue
                seen.add(key)
                out.append(NewsItem(datetime.fromisoformat(p), s, t, src, link, summ))
                picked += 1
                if picked == per_symbol:
                    break
        return out
