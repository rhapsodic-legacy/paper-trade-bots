"""The daily briefing: everything a bot is told before it decides what to trade.

It is plain text on purpose. In step 3 it becomes the body of the prompt each personality
reads, and in step 4 the journal summaries are added to it.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .account import money
from .news import MARKET, NewsCache, NewsItem
from .sim import MarketView


def pct_change(series: list[Decimal], days_back: int) -> Decimal | None:
    if len(series) <= days_back or series[-1 - days_back] == 0:
        return None
    return (series[-1] / series[-1 - days_back] - 1) * 100


def usd(value: Decimal) -> str:
    value = money(value) + 0  # + 0 turns -0.00 into 0.00
    return f"-${-value:,}" if value < 0 else f"${value:,}"


def _fmt_pct(value: Decimal | None) -> str:
    return "  n/a " if value is None else f"{value:+6.1f}%"


@dataclass
class Briefing:
    day: date
    text: str
    headlines: list[NewsItem]


def build_briefing(view: MarketView, watchlist: list[str], news: NewsCache | None = None,
                   recent_rejections: list[str] = (), memory: str = "", research: str = "",
                   system: str = "", calendar: str = "", filings: str = "") -> Briefing:
    acct, prices = view.account, view.prices
    lines = [f"# Briefing for {acct.name}, {view.day:%A %d %B %Y}", ""]

    closed = [s for s in watchlist if s not in view.open_symbols]
    lines.append("Markets open today: " + (", ".join(s for s in watchlist if s in view.open_symbols) or "none"))
    if closed:
        lines.append("Closed today (you can't trade these): " + ", ".join(closed))

    equity = acct.equity(prices)
    lines += ["", "## Your account",
              f"Cash: {usd(acct.cash)}   Total value: {usd(equity)}   "
              f"Since start: {acct.return_pct(prices):+.2f}%   Realized P&L: {usd(acct.realized_pnl)}"]
    if acct.positions:
        lines.append("Positions:")
        for s, p in sorted(acct.positions.items()):
            kind = "short" if p.quantity < 0 else "long"
            lines.append(f"- {s}: {kind} {abs(p.quantity)} at avg {usd(p.avg_price)}, now {usd(prices[s])}, "
                         f"unrealized {usd(p.unrealized_pnl(prices[s]))}")
    else:
        lines.append("Positions: none (all cash)")
    if view.notes:
        lines += ["", "## Your plans and benchmark", view.notes]

    if memory:
        lines += ["", "## Your memory (from your own journal)", memory]

    lines += ["", "## Prices", "Symbol      Close    1 day   5 days  20 days"]
    for s in watchlist:
        if s not in prices:
            lines.append(f"{s:<8} no data")
            continue
        h = view.history(s, 21)
        lines.append(f"{s:<8} {prices[s]:>10,} {_fmt_pct(pct_change(h, 1))} {_fmt_pct(pct_change(h, 5))} "
                     f"{_fmt_pct(pct_change(h, 20))}")

    if system:
        lines += ["", "## Your trading system (built in the strategy lab)", system]

    if calendar:
        lines += ["", "## Coming up (market calendar)", calendar]
    if filings:
        lines += ["", "## SEC filings this past week", filings]

    # Research desk notes replace raw headlines when the desks have published today.
    headlines = [] if research else (news.headlines(view.day, watchlist) if news else [])
    if research:
        lines += ["", "## Research desk notes", research]
    else:
        lines += ["", "## News"]
    if not research and not headlines:
        lines.append("No headlines available for today.")
    for item in headlines:
        tag = "Market" if item.symbol == MARKET else item.symbol
        lines.append(f"- [{tag}] {item.title} ({item.source}, {item.published:%d %b %H:%M} UTC)")

    if recent_rejections:
        lines += ["", "## Problems with your last orders"] + [f"- {r}" for r in recent_rejections]

    return Briefing(view.day, "\n".join(lines), headlines)
