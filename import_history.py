"""Load long price histories into history.db for the strategy lab.

    python import_history.py --csv ~/Downloads/kaggle_stocks/     # a Kaggle CSV file or folder
    python import_history.py --yahoo --years 10                   # download from Yahoo (needs internet)
    python import_history.py --synthetic --years 8                # made-up random-walk data, for trying the lab offline
    python import_history.py                                      # just show what's stored

Only the symbols the bots trade are kept by default; add --all to keep every symbol in the CSVs.
Good Kaggle starting points: "Huge Stock Market Dataset" (one file per ticker), "S&P 500 stock
data" (all_stocks_5yr.csv), and "Cryptocurrency Historical Prices" (coin_Bitcoin.csv etc.).
Download them from kaggle.com yourself; this script only reads the files.
"""
import argparse
from datetime import date, timedelta

from papertrade.history import HistoryStore
from papertrade.prices import SyntheticPriceFeed
from papertrade.universe import ASSETS, START_PRICES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="history.db")
    ap.add_argument("--csv", metavar="PATH", help="a CSV file or a folder of them")
    ap.add_argument("--all", action="store_true", help="keep every symbol found in the CSVs")
    ap.add_argument("--yahoo", action="store_true", help="download adjusted daily history from Yahoo")
    ap.add_argument("--synthetic", action="store_true", help="generate random-walk history (clearly fake)")
    ap.add_argument("--years", type=int, default=10)
    args = ap.parse_args()

    store = HistoryStore(args.db)
    if args.csv:
        only = None if args.all else {a.symbol for a in ASSETS}
        counts, warnings = store.import_csv(args.csv, only)
        for w in warnings:
            print("  " + w)
        print(f"Imported {sum(counts.values()):,} rows for {len(counts)} symbols from {args.csv}")
    if args.yahoo:
        for symbol, err in store.import_yahoo(ASSETS, args.years).items():
            print(f"  Yahoo failed for {symbol}: {err}")
    if args.synthetic:
        start = date.today() - timedelta(days=365 * args.years)
        feed = SyntheticPriceFeed(ASSETS, START_PRICES, start, 365 * args.years, seed=2024)
        rows = [(a.symbol, d, c, None) for a in ASSETS for d, c in feed.bars(a.symbol, date.today(), 10**6)]
        store.add(rows, "synthetic")
        print(f"Generated {len(rows):,} synthetic rows. These are random walks, not real prices.")

    print("\nStored history:")
    for symbol, first, last, n in store.symbols():
        print(f"  {symbol:<6} {first} to {last}  {n:>6,} days")


if __name__ == "__main__":
    main()
