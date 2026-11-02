"""Collects everything the dashboard shows from a ledger, as plain JSON-ready data."""
from decimal import Decimal

from .account import money
from .desk import DESKS, ResearchDesk
from .journal import JournalMemory
from .lab import active_system
from .ledger import Ledger
from .personas import BY_NAME
from .sim import BENCHMARK
from .standing import StandingOrders

def _assets():
    from .universe import ASSETS
    return ASSETS


def _f(x: Decimal) -> float:
    return float(money(x))


def collect(ledger: Ledger, trades_shown: int = 25, feed_shown: int = 60) -> dict:
    memory = JournalMemory(ledger)
    names = ledger.account_names()
    order = {p: i for i, p in enumerate(BY_NAME)}  # keep each bot's colour stable across reports
    names.sort(key=lambda n: order.get(n, len(order)))
    days = sorted({d for n in names for d, _, _ in ledger.snapshots(n)})
    last = days[-1] if days else None
    prices = ledger.marks(last) if last else {}

    bots = []
    for slot, name in enumerate(names):
        account = ledger.load_account(name)
        snaps = ledger.snapshots(name)
        by_day = {d: e for d, _, e in snaps}
        equity = snaps[-1][2] if snaps else account.starting_cash
        prev = snaps[-2][2] if len(snaps) > 1 else account.starting_cash
        holdings = []
        for symbol, pos in sorted(account.positions.items()):
            price = prices.get(symbol, pos.avg_price)
            holdings.append({"symbol": symbol, "quantity": str(pos.quantity.normalize()),
                             "side": "short" if pos.quantity < 0 else "long",
                             "avg_price": _f(pos.avg_price), "price": float(price),
                             "value": _f(pos.market_value(price)), "pnl": _f(pos.unrealized_pnl(price))})
        persona = BY_NAME.get(name)
        journal = ledger.journal(name)
        bots.append({
            "name": name, "slot": slot + 1,
            "tagline": persona.tagline if persona else "",
            "equity": _f(equity), "cash": _f(account.cash),
            "return_pct": float((equity / account.starting_cash - 1) * 100),
            "day_change": _f(equity - prev),
            "realized": _f(account.realized_pnl), "trade_count": len(account.trades),
            "market_view": journal[-1][1] if journal else "",
            "series": [by_day.get(d) and _f(by_day[d]) for d in days],
            "holdings": holdings,
            "trades": [{"day": t.day.isoformat(), "side": t.side, "symbol": t.symbol,
                        "quantity": str(t.quantity.normalize()), "price": float(t.price),
                        "pnl": _f(t.realized_pnl), "reason": t.reason}
                       for t in reversed(account.trades[-trades_shown:])],
            "system": active_system(ledger.db, name),
            "feed": [{"day": d.isoformat(), "kind": kind, "title": title, "text": text}
                     for d, kind, title, text in memory.feed(name)[:feed_shown]],
        })
    desk = ResearchDesk(ledger, None, assets=_assets())
    desks = []
    for d in DESKS:
        latest = desk.reports(d.name, last)[-1:] if last else []
        if not latest:
            continue
        r = latest[0]
        record = desk.track_record(d.name, last)
        desks.append({"name": d.name, "day": r.day.isoformat(), "headline": r.headline, "summary": r.summary,
                      "opinion": r.opinion, "dissent": r.dissent, "calls": r.calls,
                      "sources": [x for x in r.sources if str(x.get("link", "")).startswith(("https://", "http://"))],
                      "record": record["text"], "hit_rate": record["right"] / record["scored"] if record["scored"] else None})
    starting = float(ledger.load_account(names[0]).starting_cash) if names else 100000.0
    return {"days": [d.isoformat() for d in days], "as_of": last.isoformat() if last else None,
            "starting_cash": starting, "bots": bots, "desks": desks,
            "benchmark": benchmark(ledger, days, starting), "standing": standing_orders(ledger, names)}


def benchmark(ledger: Ledger, days: list, starting: float) -> dict | None:
    """Putting all the starting cash into SPY on the first day and holding: the bar every bot has to clear."""
    closes = [ledger.marks(d).get(BENCHMARK) for d in days]
    if not closes or closes[0] is None:
        return None
    series = [round(starting * float(c / closes[0]), 2) if c is not None else None for c in closes]
    last = next(v for v in reversed(series) if v is not None)
    prev = next((v for v in reversed(series[:-1]) if v is not None), starting)
    return {"name": "S&P 500 buy-and-hold", "series": series, "equity": last,
            "return_pct": (last / starting - 1) * 100, "day_change": round(last - prev, 2)}


def standing_orders(ledger: Ledger, names: list[str]) -> list[dict]:
    book = StandingOrders(ledger.db)
    return [{"bot": n, "order": o.describe(), "reason": o.reason} for n in names for o in book.open_orders(n)]
