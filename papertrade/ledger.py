import sqlite3
from datetime import date
from decimal import Decimal

from .account import Account, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    starting_cash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    account TEXT NOT NULL REFERENCES accounts(name),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    day TEXT NOT NULL,
    account TEXT NOT NULL REFERENCES accounts(name),
    cash TEXT NOT NULL,
    equity TEXT NOT NULL,
    PRIMARY KEY (day, account)
);
CREATE TABLE IF NOT EXISTS marks (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price TEXT NOT NULL,
    PRIMARY KEY (day, symbol)
);
CREATE TABLE IF NOT EXISTS journal (
    day TEXT NOT NULL,
    account TEXT NOT NULL REFERENCES accounts(name),
    market_view TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL,
    problems TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (day, account)
);
"""


class Ledger:
    """SQLite record of every account, fill and end-of-day snapshot.

    Decimals are stored as text so no cents are lost to floating point.
    """

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def add_account(self, account: Account) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?)",
                            (account.name, str(account.starting_cash)))

    def record_trade(self, t: Trade) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO trades (day, account, symbol, side, quantity, price, fee, realized_pnl, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t.day.isoformat(), t.account, t.symbol, t.side, str(t.quantity), str(t.price),
                 str(t.fee), str(t.realized_pnl), t.reason))

    def record_snapshot(self, day: date, account: str, cash: Decimal, equity: Decimal) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)",
                            (day.isoformat(), account, str(cash), str(equity)))

    def record_marks(self, day: date, prices) -> None:
        """The closing prices used to value accounts that day, so reports need only the ledger."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO marks VALUES (?, ?, ?)",
                                [(day.isoformat(), s, str(p)) for s, p in prices.items()])

    def marks(self, day: date) -> dict[str, Decimal]:
        """Prices from the latest marked day on or before `day`."""
        row = self.db.execute("SELECT MAX(day) FROM marks WHERE day <= ?", (day.isoformat(),)).fetchone()
        if not row or row[0] is None:
            return {}
        return {s: Decimal(p) for s, p in self.db.execute("SELECT symbol, price FROM marks WHERE day = ?", row)}

    def record_journal(self, day: date, account: str, entry: str, market_view: str = "",
                       problems: list[str] = ()) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO journal VALUES (?, ?, ?, ?, ?)",
                            (day.isoformat(), account, market_view, entry, "\n".join(problems)))

    def journal(self, account: str) -> list[tuple[date, str, str]]:
        """(day, market_view, entry) for one bot, oldest first."""
        rows = self.db.execute("SELECT day, market_view, entry FROM journal WHERE account = ? ORDER BY day",
                               (account,))
        return [(date.fromisoformat(d), v, e) for d, v, e in rows]

    def last_problems(self, account: str) -> list[str]:
        row = self.db.execute("SELECT problems FROM journal WHERE account = ? ORDER BY day DESC LIMIT 1",
                              (account,)).fetchone()
        return [p for p in row[0].split("\n") if p] if row else []

    def trades(self, account: str | None = None) -> list[Trade]:
        sql = "SELECT day, account, symbol, side, quantity, price, fee, realized_pnl, reason FROM trades"
        args = ()
        if account:
            sql, args = sql + " WHERE account = ?", (account,)
        rows = self.db.execute(sql + " ORDER BY id", args)
        return [Trade(date.fromisoformat(d), a, s, side, Decimal(q), Decimal(p), Decimal(f), Decimal(r), why)
                for d, a, s, side, q, p, f, r, why in rows]

    def snapshots(self, account: str) -> list[tuple[date, Decimal, Decimal]]:
        rows = self.db.execute("SELECT day, cash, equity FROM snapshots WHERE account = ? ORDER BY day",
                               (account,))
        return [(date.fromisoformat(d), Decimal(c), Decimal(e)) for d, c, e in rows]

    def account_names(self) -> list[str]:
        return [n for (n,) in self.db.execute("SELECT name FROM accounts ORDER BY rowid")]

    def load_account(self, name: str) -> Account:
        """Rebuild an account by replaying its trades, so a sim can resume where it stopped."""
        row = self.db.execute("SELECT starting_cash FROM accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        account = Account(name, Decimal(row[0]))
        for trade in self.trades(name):
            account.apply(trade)
        return account
