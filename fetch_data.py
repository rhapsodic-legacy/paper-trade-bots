"""Download real prices and headlines into data.db, then print a sample briefing.

    python fetch_data.py                 # last 400 days of prices, today's news
    python fetch_data.py --days 365      # a longer price history for backtests

Run it once a day to keep building the news archive (RSS only shows recent stories).
"""
import argparse
import sys
from datetime import date, timedelta

from papertrade import Account, Simulation
from papertrade.briefing import build_briefing
from papertrade.market_data import PriceCache
from papertrade.news import NewsCache
from papertrade.desk import ResearchDesk
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

    sim = Simulation(ASSETS, prices.feed())
    view = sim.view(today, Account("Sample Bot"))
    print()
    print(build_briefing(view, [a.symbol for a in ASSETS], news).text)


if __name__ == "__main__":
    main()
