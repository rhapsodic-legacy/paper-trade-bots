"""Standing orders and trade theses: the plans a bot makes that the simulator holds it to.

A bot that writes "my stop is $323" in its journal has made a wish, not an order. Here it can
place real ones that are checked at every daily close, whether or not the bot is called that day:

  stop_loss    close a position when the close moves against it past `price`
  take_profit  close a position when the close moves in its favour past `price`
  limit_buy    buy when the close falls to `price` or below (expires after a few days)
  limit_short  short when the close rises to `price` or above (expires after a few days)

Only daily closes are known, so an order triggers on the close that crosses its price and fills at
that close, which can be worse than `price` after a gap. The bots are told so.

Every buy or short also carries an invalidation: one sentence on what would prove the trade
wrong. It's shown back to the bot every day while it holds the position.
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Mapping

from .broker import Order, Side

KINDS = ("stop_loss", "take_profit", "limit_buy", "limit_short")
PROTECTIVE = ("stop_loss", "take_profit")
LIMIT_DAYS = 10  # default life of a limit order

SCHEMA = """
CREATE TABLE IF NOT EXISTS standing_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    price TEXT NOT NULL,
    amount TEXT NOT NULL,
    unit TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    expires TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    closed TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS theses (
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    side TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    PRIMARY KEY (account, symbol, day, side)
);
"""


@dataclass(frozen=True)
class StandingOrder:
    """What a strategy returns, alongside ordinary Orders, to place or cancel a standing order.

    price 0 cancels the account's open order of that kind for that symbol."""
    symbol: str
    kind: str
    price: Decimal
    amount: Decimal = Decimal("0")
    unit: str = "all"  # all | units | usd
    reason: str = ""
    expires_days: int = LIMIT_DAYS


@dataclass(frozen=True)
class Placed:
    id: int
    account: str
    symbol: str
    kind: str
    price: Decimal
    amount: Decimal
    unit: str
    reason: str
    created: date
    expires: date | None

    def describe(self) -> str:
        size = "the whole position" if self.unit == "all" else (
            f"${self.amount:,}" if self.unit == "usd" else f"{self.amount.normalize()} units")
        words = {"stop_loss": "stop-loss", "take_profit": "take-profit", "limit_buy": "limit buy",
                 "limit_short": "limit short"}[self.kind]
        until = f", until {self.expires:%d %b}" if self.expires else ""
        return f"{words} on {self.symbol} at ${self.price:,} for {size}{until}"


class StandingOrders:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    # ----- placing -----

    def place(self, account: str, day: date, o: StandingOrder) -> str:
        """Place (or replace, or cancel) one order. Returns a line for the log."""
        if o.kind not in KINDS:
            return f"Unknown standing order kind {o.kind!r}"
        with self.db:
            self.db.execute("UPDATE standing_orders SET status = 'replaced', closed = ? "
                            "WHERE account = ? AND symbol = ? AND kind = ? AND status = 'open'",
                            (day.isoformat(), account, o.symbol, o.kind))
            if o.price <= 0:
                return f"Cancelled {o.kind.replace('_', ' ')} on {o.symbol}"
            expires = None if o.kind in PROTECTIVE else day + timedelta(days=max(1, o.expires_days))
            self.db.execute(
                "INSERT INTO standing_orders (account, symbol, kind, price, amount, unit, reason, created, expires)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account, o.symbol, o.kind, str(o.price), str(o.amount), o.unit, o.reason, day.isoformat(),
                 expires.isoformat() if expires else None))
        return "Placed " + self.open_orders(account)[-1].describe()

    def open_orders(self, account: str) -> list[Placed]:
        rows = self.db.execute(
            "SELECT id, account, symbol, kind, price, amount, unit, reason, created, expires FROM standing_orders"
            " WHERE account = ? AND status = 'open' ORDER BY id", (account,))
        return [Placed(i, a, s, k, Decimal(p), Decimal(am), u, r, date.fromisoformat(c),
                       date.fromisoformat(e) if e else None) for i, a, s, k, p, am, u, r, c, e in rows]

    def close(self, order_id: int, status: str, day: date, note: str = "") -> None:
        with self.db:
            self.db.execute("UPDATE standing_orders SET status = ?, closed = ?, note = ? WHERE id = ?",
                            (status, day.isoformat(), note, order_id))

    def history(self, account: str, since: date) -> list[tuple[str, str, str, str, str]]:
        """(closed day, status, kind, symbol, note) for orders that fired, failed or expired since `since`."""
        return list(self.db.execute(
            "SELECT closed, status, kind, symbol, note FROM standing_orders WHERE account = ? AND closed >= ?"
            " AND status IN ('filled', 'failed', 'expired') ORDER BY id", (account, since.isoformat())))

    # ----- checking at the close -----

    def due(self, account, day: date, prices: Mapping[str, Decimal],
            open_symbols) -> list[tuple[Placed, Order]]:
        """Orders whose trigger the day's close has crossed, as market orders to fill at that close."""
        out = []
        for o in self.open_orders(account.name):
            if o.expires and day > o.expires:
                self.close(o.id, "expired", day, f"{o.describe()} expired unfilled")
                continue
            if o.symbol not in open_symbols or o.symbol not in prices:
                continue
            close, held = prices[o.symbol], account.position(o.symbol).quantity
            if o.kind in PROTECTIVE:
                if held == 0:
                    self.close(o.id, "cancelled", day, "position already closed")
                    continue
                long = held > 0
                hit = (close <= o.price if long else close >= o.price) if o.kind == "stop_loss" else \
                      (close >= o.price if long else close <= o.price)
                if not hit:
                    continue
                qty = abs(held) if o.unit == "all" else min(abs(held), self._qty(o, close))
                side = Side.SELL if long else Side.COVER
            elif o.kind == "limit_buy":
                if close > o.price:
                    continue
                qty, side = self._qty(o, close), Side.BUY
            else:  # limit_short
                if close < o.price:
                    continue
                qty, side = self._qty(o, close), Side.SHORT
            label = o.kind.replace("_", "-")
            reason = f"[{label} at ${o.price:,}, closed at ${close:,}] {o.reason}".strip()
            out.append((o, Order(o.symbol, side, qty, reason)))
        return out

    @staticmethod
    def _qty(o: Placed, price: Decimal) -> Decimal:
        if o.unit == "usd":
            return (o.amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        return o.amount

    # ----- theses -----

    def record_thesis(self, account: str, symbol: str, day: date, side: str, invalidation: str) -> None:
        if not invalidation.strip():
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO theses VALUES (?, ?, ?, ?, ?)",
                            (account, symbol, day.isoformat(), side, invalidation.strip()))

    def theses(self, account: str) -> dict[str, list[tuple[date, str, str]]]:
        """symbol -> [(day, side, invalidation)], oldest first."""
        out: dict[str, list] = {}
        for s, d, side, text in self.db.execute(
                "SELECT symbol, day, side, invalidation FROM theses WHERE account = ? ORDER BY day", (account,)):
            out.setdefault(s, []).append((date.fromisoformat(d), side, text))
        return out

    # ----- what the bot is told -----

    def notes(self, account, day: date) -> str:
        lines = []
        held = account.positions
        theses = self.theses(account.name)
        for symbol, pos in sorted(held.items()):
            want = "buy" if pos.quantity > 0 else "short"
            mine = [t for t in theses.get(symbol, []) if t[1] == want]
            if mine:
                d, _, text = mine[-1]
                lines.append(f"- {symbol}: when you opened it on {d:%d %b} you said you'd be wrong if: {text}")
        if lines:
            lines.insert(0, "What would prove you wrong (check each one: if it has happened, act or explain why not):")
        orders = self.open_orders(account.name)
        if orders:
            lines.append("Your standing orders (checked at every close, even on days you aren't asked):")
            lines += [f"- {o.describe()}" + (f": {o.reason}" if o.reason else "") for o in orders]
        else:
            lines.append("Standing orders: none. Positions without a stop-loss can fall any distance.")
        fired = self.history(account.name, day - timedelta(days=3))
        if fired:
            lines.append("Recently triggered or expired:")
            lines += [f"- {d}: {note}" for d, _, _, _, note in fired]
        return "\n".join(lines)
