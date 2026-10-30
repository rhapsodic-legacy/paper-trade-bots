"""Run two simulated weeks with placeholder rule-based versions of the seven bots.

These rules only exercise the engine. In step 3 each bot's decide() is replaced
by Claude reading its daily briefing and journal.

    python demo.py            # fresh run in memory
    python demo.py demo.db    # keep the ledger in a SQLite file
"""
import sys
from datetime import date, timedelta
from decimal import Decimal as D

from papertrade.briefing import build_briefing
from papertrade import Ledger, Order, Side, Simulation, SyntheticPriceFeed
from papertrade.universe import ASSETS, START_PRICES


def spend(view, symbol, fraction, reason):
    """Buy `fraction` of current equity worth of `symbol`."""
    qty = view.equity * D(fraction) / view.prices[symbol]
    return Order(symbol, Side.BUY, qty, reason)


def once(orders_fn):
    """Bots that set up a portfolio on their first open day and then hold."""
    def strategy(view):
        if view.account.positions or not set(view.open_symbols) >= {"SPY"}:
            return []
        return orders_fn(view)
    return strategy


def momentum(view):
    orders = []
    for s in ("NVDA", "AAPL", "MSFT"):
        if s not in view.open_symbols:
            continue
        h = view.history(s, 4)
        held = view.account.position(s).quantity
        if len(h) == 4 and h[-1] > h[0] and held == 0:
            orders.append(spend(view, s, "0.3", f"{s} up 3 days running"))
        elif len(h) == 4 and h[-1] < h[0] and held > 0:
            orders.append(Order(s, Side.SELL, held, f"{s} trend broke"))
    return orders


def contrarian(view):
    if "SPY" not in view.open_symbols:
        return []
    h = view.history("SPY", 2)
    held = view.account.position("SPY").quantity
    if len(h) < 2:
        return []
    if h[-1] > h[0] and held >= 0:  # market up: bet it falls
        orders = [Order("SPY", Side.SELL, held, "closing long")] if held > 0 else []
        return orders + [Order("SPY", Side.SHORT, view.equity * D("0.5") / view.prices["SPY"], "everyone's too happy")]
    if h[-1] < h[0] and held <= 0:  # market down: bet it rises
        orders = [Order("SPY", Side.COVER, -held, "closing short")] if held < 0 else []
        return orders + [spend(view, "SPY", "0.5", "everyone's too scared")]
    return []


def bitcoin_bob(view):
    if view.account.positions:
        return []
    return [spend(view, "BTC", "0.8", "stack sats"), spend(view, "ETH", "0.15", "a little ETH")]


BOTS = {
    "Cautious Retiree": once(lambda v: [spend(v, "KO", "0.2", "dividends"), spend(v, "JNJ", "0.2", "steady"),
                                        spend(v, "SPY", "0.2", "the index")]),
    "Meme-Stock Gambler": once(lambda v: [spend(v, "GME", "0.95", "to the moon")]),
    "Value Investor": once(lambda v: [spend(v, "KO", "0.45", "moat"), spend(v, "JNJ", "0.45", "cheap")]),
    "Momentum Trader": momentum,
    "Tech Growth Bull": once(lambda v: [spend(v, s, "0.32", "AI boom") for s in ("NVDA", "MSFT", "AAPL")]),
    "Contrarian Carl": contrarian,
    "Bitcoin Bob": bitcoin_bob,
}


def main():
    start, days = date(2026, 10, 12), 14
    feed = SyntheticPriceFeed(ASSETS, START_PRICES, start, days, seed=7)
    sim = Simulation(ASSETS, feed, Ledger(sys.argv[1] if len(sys.argv) > 1 else ":memory:"))
    for i in range(days):
        day = start + timedelta(days=i)
        result = sim.step(day, BOTS)
        for t in result.fills:
            print(f"{day} {t.account:<19} {t.side:<5} {t.quantity:>12} {t.symbol:<5} @ {t.price:>9}  ({t.reason})")
        for name, order, why in result.rejections:
            print(f"{day} {name:<19} REJECTED {order.side.value} {order.symbol}: {why}")
    last = start + timedelta(days=days - 1)
    print(f"\nLeaderboard on {last}:")
    for rank, (name, equity, pct) in enumerate(sim.leaderboard(last), 1):
        print(f"{rank}. {name:<19} ${equity:>12,}  {pct:+}%")

    # What a bot will read each morning once it's driven by Claude (no news offline).
    carl = sim.accounts["Contrarian Carl"]
    print()
    print(build_briefing(sim.view(last, carl), ["SPY", "AAPL", "NVDA", "BTC"]).text)


if __name__ == "__main__":
    main()
