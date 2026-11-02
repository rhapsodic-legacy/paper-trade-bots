"""Journal mining: once a month, find out which kinds of reasoning made money and which lost it.

Every trade the bots close is turned into a round trip: why it was opened (the bot's own words,
and what it said would prove it wrong), why it was closed, how long it was held and what it made.
Claude reads them all and labels each opening reason with one category. The win rates and
profits per category are then counted here, in code, so the numbers are exact; Claude only adds
the patterns it sees and one to three lessons per bot, which go back into each bot's memory.

It pays off slowly: a month in there's little to mine, but a year in it's an honest record of how
each kind of reasoning actually performed, which few human traders ever keep.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .journal import month_bounds
from .ledger import Ledger
from .standing import StandingOrders

CATEGORIES = ["trend or momentum", "dip-buying or contrarian", "valuation or fundamentals",
              "news or event reaction", "research desk call", "macro view", "trading-system signal",
              "gut feel or hype"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS mined (
    month TEXT PRIMARY KEY,      -- first day of the month mined
    body TEXT NOT NULL,          -- JSON: trips, labels, stats, patterns, lessons
    written_by TEXT NOT NULL
);
"""

MINING_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {"type": "array", "items": {
            "type": "object",
            "properties": {"trip": {"type": "integer"}, "category": {"type": "string", "enum": CATEGORIES}},
            "required": ["trip", "category"], "additionalProperties": False}},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "by_bot": {"type": "array", "items": {
            "type": "object",
            "properties": {"bot": {"type": "string"}, "lessons": {"type": "array", "items": {"type": "string"}}},
            "required": ["bot", "lessons"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["labels", "patterns", "by_bot", "summary"],
    "additionalProperties": False,
}

MINER_SYSTEM = """You are the performance coach for Paper_Trade, an educational simulator where seven AI \
trading bots with different personalities trade pretend money. Once a month you study every round trip \
they closed (and positions still open) to find out which kinds of reasoning actually make money. People \
read your findings to learn about trading and about AI's limits, so be blunt, specific and fair: one \
month is a tiny sample, and luck is often the best explanation. Never invent numbers; the profit figures \
you're given are exact.

Label each numbered trip by the main reason it was OPENED, using exactly one category. Then give 2 to 5 \
patterns you see across all bots (say when a sample is too small to mean much), 1 to 3 concrete lessons \
for each bot that traded, written to that bot as "you", and a 2 to 4 sentence summary."""


@dataclass(frozen=True)
class Trip:
    account: str
    symbol: str
    direction: str  # long | short
    opened: date
    closed: date | None  # None: still open at the end of the period
    entry_reasons: list[str]
    invalidation: str
    exit_reason: str
    pnl: Decimal
    pct: Decimal

    @property
    def won(self) -> bool:
        return self.pnl > 0


def round_trips(ledger: Ledger, account: str, start: date, end: date, theses: dict | None = None) -> list[Trip]:
    """Closed pieces of positions (each sell or cover) in [start, end], plus positions open at `end`."""
    trips, book = [], {}  # symbol -> [qty, cost, opened, reasons, direction]
    theses = theses or {}
    marks = ledger.marks(end)
    for t in ledger.trades(account):
        if t.day > end:
            break
        pos = book.get(t.symbol)
        if t.side in ("buy", "short"):
            if pos is None:
                pos = book[t.symbol] = [Decimal(0), Decimal(0), t.day, [], "long" if t.side == "buy" else "short"]
            pos[0] += t.quantity
            pos[1] += t.quantity * t.price
            if t.reason:
                pos[3].append(f"{t.day:%d %b}: {t.reason}")
            continue
        if pos is None or pos[0] <= 0:
            continue
        avg = pos[1] / pos[0]
        if start <= t.day:
            said = [x for d, side, x in theses.get(t.symbol, [])
                    if pos[2] <= d <= t.day and side == ("buy" if pos[4] == "long" else "short")]
            trips.append(Trip(account, t.symbol, pos[4], pos[2], t.day, list(pos[3]), said[-1] if said else "",
                              t.reason, t.realized_pnl, t.realized_pnl / (avg * t.quantity) * 100))
        pos[0] -= t.quantity
        pos[1] -= avg * t.quantity
        if pos[0] <= 0:
            del book[t.symbol]
    for symbol, (qty, cost, opened, reasons, direction) in book.items():
        if symbol not in marks or qty <= 0:
            continue
        value = qty * marks[symbol]
        pnl = value - cost if direction == "long" else cost - value
        said = [x for d, side, x in theses.get(symbol, []) if d >= opened]
        trips.append(Trip(account, symbol, direction, opened, None, reasons, said[-1] if said else "",
                          "(still open)", pnl, pnl / cost * 100))
    return trips


def stats(trips: list[Trip], labels: dict[int, str]) -> list[dict]:
    """Per category: trips, wins, total and average profit. Counted in code, so they're exact."""
    out = []
    for cat in CATEGORIES:
        mine = [t for i, t in enumerate(trips, 1) if labels.get(i) == cat]
        if mine:
            total = sum((t.pnl for t in mine), Decimal(0))
            out.append({"category": cat, "trips": len(mine), "wins": sum(t.won for t in mine),
                        "total_pnl": float(round(total, 2)), "avg_pct": float(round(sum(t.pct for t in mine) / len(mine), 2))})
    return sorted(out, key=lambda r: -r["total_pnl"])


class JournalMiner:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.db: sqlite3.Connection = ledger.db
        self.db.executescript(SCHEMA)
        self.theses = StandingOrders(ledger.db)
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def trips(self, start: date, end: date) -> list[Trip]:
        return [t for name in self.ledger.account_names()
                for t in round_trips(self.ledger, name, start, end, self.theses.theses(name))]

    def message(self, label: str, trips: list[Trip]) -> str:
        lines = [f"# Round trips for {label}", ""]
        for i, t in enumerate(trips, 1):
            held = f"{t.opened:%d %b} to {t.closed:%d %b}" if t.closed else f"opened {t.opened:%d %b}, still open"
            lines += [f"{i}. {t.account}: {t.direction} {t.symbol}, {held}, profit ${t.pnl:,.2f} ({t.pct:+.1f}%)",
                      "   Why opened: " + (" | ".join(t.entry_reasons) or "(no reason given)")]
            if t.invalidation:
                lines.append(f"   Said it would be wrong if: {t.invalidation}")
            lines.append(f"   Why closed: {t.exit_reason}")
        return "\n".join(lines)

    def mine(self, brain, today: date) -> list[dict]:
        """Mine every finished month with trades that hasn't been mined yet. Returns what was written."""
        done = {m for (m,) in self.db.execute("SELECT month FROM mined WHERE written_by = 'claude'")}
        days = sorted({t.day for t in self.ledger.trades()})
        written = []
        for start, end in sorted({month_bounds(d) for d in days}):
            if end >= today or start.isoformat() in done:
                continue
            trips = self.trips(start, end)
            if not trips:
                continue
            label = f"{start:%B %Y}"
            raw, problem, usage = brain.ask(MINER_SYSTEM, self.message(label, trips), MINING_SCHEMA)
            for k in self.usage:
                self.usage[k] += usage.get(k, 0)
            labels = {}
            if raw:
                labels = {int(l["trip"]): l["category"] for l in raw.get("labels", [])
                          if isinstance(l.get("trip"), int) and l.get("category") in CATEGORIES}
            body = {"month": label, "summary": (raw or {}).get("summary", f"Not mined: {problem}"),
                    "patterns": (raw or {}).get("patterns", []),
                    "lessons": {b["bot"]: b["lessons"][:3] for b in (raw or {}).get("by_bot", [])},
                    "stats": stats(trips, labels),
                    "trips": [{"bot": t.account, "symbol": t.symbol, "direction": t.direction,
                               "opened": t.opened.isoformat(), "closed": t.closed.isoformat() if t.closed else None,
                               "pnl": float(round(t.pnl, 2)), "pct": float(round(t.pct, 2)),
                               "category": labels.get(i, "")} for i, t in enumerate(trips, 1)]}
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO mined VALUES (?, ?, ?)",
                                (start.isoformat(), json.dumps(body), "claude" if raw else "facts only"))
            written.append(body)
        return written

    def reports(self) -> list[dict]:
        return [json.loads(b) for (b,) in self.db.execute("SELECT body FROM mined ORDER BY month")]


def lessons_for(db: sqlite3.Connection, account: str) -> str:
    """The latest monthly mining, as a bot reads it: its own lessons and the scoreboard of reasoning."""
    try:
        row = db.execute("SELECT body FROM mined WHERE written_by = 'claude' ORDER BY month DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # never mined yet
        return ""
    if not row:
        return ""
    body = json.loads(row[0])
    lines = [f"What the coach found mining everyone's trades from {body['month']} (small samples, so hold loosely):"]
    for s in body["stats"]:
        lines.append(f"- {s['category']}: {s['wins']} of {s['trips']} won, ${s['total_pnl']:,.0f} total")
    mine = body["lessons"].get(account, [])
    if mine:
        lines.append("The coach's lessons for you:")
        lines += [f"- {l}" for l in mine]
    return "\n".join(lines)
