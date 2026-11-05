"""Download real prices and headlines into data.db, then print a sample briefing.

    python fetch_data.py                 # last 400 days of prices, today's news
    python fetch_data.py --days 365      # a longer price history for backtests

Run it once a day to keep building the news archive (RSS only shows recent stories).
"""
import argparse
import sys
from datetime import date, timedelta

from papertrade import Account, AssetClass, Simulation
from papertrade.briefing import build_briefing
from papertrade.market_data import PriceCache
from papertrade.news import NewsCache
from papertrade.desk import ResearchDesk
from papertrade.events import MarketCalendar
from papertrade.filings import FilingsClerk
from papertrade.universe import ASSETS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data.db")
    ap.add_argument("--days", type=int, default=400, help="price history to keep (lab systems use up to 250 days)")
    args = ap.parse_args()

    today = date.today()
    prices, news = PriceCache(args.db), NewsCache(args.db)
    print(f"Fetching {args.days} days of prices for {len(ASSETS)} symbols...")
    failed = prices.update(ASSETS, today - timedelta(days=args.days), today)
    for symbol, err in failed.items():
        print(f"  price fetch failed for {symbol}: {err}")
    if len(failed) == len(ASSETS):
        sys.exit("No prices could be fetched (offline?). Nothing to trade on today.")
    print("Fetching headlines...")
    errors = news.update(ASSETS)
    errors.update(news.update_topics(ResearchDesk.topics()))
    if errors:
        url, err = next(iter(errors.items()))
        print(f"  {len(errors)} news feeds failed, e.g. {url}: {err}")

    stocks = [a.symbol for a in ASSETS if a.asset_class is AssetClass.STOCK]
    print("Fetching the market calendar (earnings, Fed, inflation, jobs)...")
    calendar = MarketCalendar(args.db)
    errors = calendar.update(stocks, today)
    if errors:
        url, err = next(iter(errors.items()))
        print(f"  {len(errors)} calendar sources failed (built-in Fed/BLS dates still apply), e.g. {url}: {err}")
    print("Fetching SEC filings...")
    filings = FilingsClerk(args.db)
    errors = filings.update(stocks, today)
    if errors:
        what, err = next(iter(errors.items()))
        print(f"  {len(errors)} filing lookups failed, e.g. {what}: {err}")

    sim = Simulation(ASSETS, prices.feed())
    view = sim.view(today, Account("Sample Bot"))
    watch = [a.symbol for a in ASSETS]
    print()
    print(build_briefing(view, watch, news, calendar=calendar.notes_for(today, watch),
                         filings=filings.notes_for(today, watch)).text)


if __name__ == "__main__":
    main()
