"""Journals as memory.

Every trading day a bot writes a daily entry (saved by run_bots.py). When a week or a month is
over, it is rolled up into a summary: the numbers come straight from the ledger, and Claude writes
the story and the lessons in the bot's own voice. Each morning the bot is reminded of its recent
daily entries, its weekly and monthly summaries, and the lessons it chose to keep.

Keeping facts in code and only the narrative in Claude means a bot can misremember how it felt,
but never how much money it made.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .account import money
from .ledger import Ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    account TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'week' or 'month'
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    summary TEXT NOT NULL,
    lessons TEXT NOT NULL,       -- JSON list of strings
    facts TEXT NOT NULL,
    written_by TEXT NOT NULL,    -- 'claude', or 'facts only' if Claude couldn't be reached
    PRIMARY KEY (account, kind, start)
);
"""


@dataclass(frozen=True)
class Summary:
    account: str
    kind: str
    start: date
    end: date
    summary: str
    lessons: list[str]
    facts: str
    written_by: str

    @property
    def label(self) -> str:
        if self.kind == "month":
            return f"{self.start:%B %Y}"
        return f"week of {self.start:%d %b %Y}"


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


class JournalMemory:
    def __init__(self, ledger: Ledger, starting_cash: Decimal = Decimal("100000")):
        self.ledger = ledger
        self.db: sqlite3.Connection = ledger.db
        self.db.executescript(SCHEMA)
        self.starting_cash = Decimal(starting_cash)
        self.usage = {"input_tokens": 0, "output_tokens": 0}  # Claude tokens spent on summaries

    # ----- facts from the ledger -----

    def _equity_at(self, account: str, day: date) -> Decimal:
        """Equity at the last snapshot on or before `day` (starting cash if none yet)."""
        before = [e for d, _, e in self.ledger.snapshots(account) if d <= day]
        return before[-1] if before else self.starting_cash

    def facts(self, account: str, start: date, end: date) -> str:
        opening = self._equity_at(account, start - timedelta(days=1))
        closing = self._equity_at(account, end)
        change = (closing / opening - 1) * 100 if opening else Decimal(0)
        trades = [t for t in self.ledger.trades(account) if start <= t.day <= end]
        closed = [t for t in trades if t.side in ("sell", "cover")]
        lines = [f"Account value: {money(opening):,} at the start, {money(closing):,} at the end "
                 f"({change:+.2f}%). Overall since the very start: {(closing / self.starting_cash - 1) * 100:+.2f}%.",
                 f"Trades: {len(trades)} ({sum(t.side in ('buy', 'short') for t in trades)} opening, "
                 f"{len(closed)} closing). Realized profit/loss: {money(sum((t.realized_pnl for t in trades), Decimal(0))):,}."]
        if closed:
            best = max(closed, key=lambda t: t.realized_pnl)
            worst = min(closed, key=lambda t: t.realized_pnl)
            lines.append(f"Best closed trade: {best.side} {best.symbol} on {best.day}, {money(best.realized_pnl):,}.")
            if worst is not best:
                lines.append(f"Worst closed trade: {worst.side} {worst.symbol} on {worst.day}, {money(worst.realized_pnl):,}.")
        for t in trades:
            lines.append(f"- {t.day} {t.side} {t.quantity} {t.symbol} @ {t.price}: {t.reason}")
        return "\n".join(lines)

    # ----- reading and writing summaries -----

    def summaries(self, account: str, kind: str | None = None) -> list[Summary]:
        sql, args = "SELECT * FROM summaries WHERE account = ?", [account]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        rows = self.db.execute(sql + " ORDER BY start", args)
        return [Summary(a, k, date.fromisoformat(s), date.fromisoformat(e), text, json.loads(l), f, w)
                for a, k, s, e, text, l, f, w in rows]

    def _save(self, s: Summary) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (s.account, s.kind, s.start.isoformat(), s.end.isoformat(), s.summary,
                             json.dumps(s.lessons), s.facts, s.written_by))

    def _entries_text(self, account: str, kind: str, start: date, end: date) -> str:
        if kind == "month":  # a month is told through its weekly summaries, plus any days they miss
            weeks = [w for w in self.summaries(account, "week") if start <= w.end and w.start <= end]
            covered = {w.start + timedelta(days=i) for w in weeks for i in range(7)}
            parts = [f"[{w.label}] {w.summary}" for w in weeks]
            parts += [f"[{d}] {e}" for d, _, e in self.ledger.journal(account)
                      if start <= d <= end and d not in covered and e]
        else:
            parts = [f"[{d:%a %d %b}] {e}" for d, _, e in self.ledger.journal(account) if start <= d <= end and e]
        return "\n\n".join(parts) or "(no entries)"

    def roll_up(self, persona, brain, today: date) -> list[Summary]:
        """Write any weekly or monthly summaries that are due: periods that ended before `today`,
        that have journal entries, and that aren't summarized yet. Weeks go first so months can use them."""
        name = persona.name
        days = sorted({d for d, _, e in self.ledger.journal(name) if e and d < today})
        # Facts-only summaries (Claude was down) are retried on the next run.
        have = {(s.kind, s.start) for s in self.summaries(name) if s.written_by == "claude"}
        written = []
        for kind, bounds in (("week", week_bounds), ("month", month_bounds)):
            periods = sorted({bounds(d) for d in days})
            for start, end in periods:
                if end >= today or (kind, start) in have:
                    continue
                facts = self.facts(name, start, end)
                label = Summary(name, kind, start, end, "", [], "", "").label
                result = brain.summarize(persona, kind + "ly", label, facts,
                                         self._entries_text(name, kind, start, end))
                if result:
                    text, lessons, usage = result
                    for k in self.usage:
                        self.usage[k] += usage.get(k, 0)
                    s = Summary(name, kind, start, end, text, lessons, facts, "claude")
                else:  # keep the facts so the memory isn't blank
                    s = Summary(name, kind, start, end, "(Claude was unavailable, so only the facts were kept.)",
                                [], facts, "facts only")
                self._save(s)
                written.append(s)
        return written

    # ----- what the bot is reminded of each morning -----

    def recall(self, account: str, today: date, days: int = 5, weeks: int = 3, months: int = 3) -> str:
        parts = []
        monthly = [s for s in self.summaries(account, "month") if s.end < today][-months:]
        weekly = [s for s in self.summaries(account, "week") if s.end < today][-weeks:]
        lessons = [l for s in monthly + weekly for l in s.lessons]
        if lessons:
            # Newest first, without repeats, so the freshest version of a lesson wins.
            seen, kept = set(), []
            for l in reversed(lessons):
                if l.lower() not in seen:
                    seen.add(l.lower())
                    kept.append(l)
            parts.append("Lessons you chose to keep:\n" + "\n".join(f"- {l}" for l in kept[:8]))
        for s in monthly:
            parts.append(f"Your summary of {s.label}:\n{s.summary}")
        for s in weekly:
            parts.append(f"Your summary of the {s.label}:\n{s.summary}")
        from .mining import lessons_for  # imported here: mining builds on this module
        coached = lessons_for(self.db, account)
        if coached:
            parts.append(coached)
        recent = [(d, v, e) for d, v, e in self.ledger.journal(account) if d < today and e][-days:]
        if recent:
            parts.append("Your last few daily entries:\n" + "\n".join(
                f"- {d:%a %d %b}: {e}" for d, _, e in recent))
        return "\n\n".join(parts)

    def last_problems(self, account: str) -> list[str]:
        return self.ledger.last_problems(account)

    # ----- the feed people read -----

    def feed(self, account: str) -> list[tuple[date, str, str, str]]:
        """Everything the bot has written, newest first: (date, kind, title, text)."""
        items = [(d, "day", f"{d:%A %d %B %Y}" + (f" · {v}" if v else ""), e)
                 for d, v, e in self.ledger.journal(account) if e]
        for s in self.summaries(account):
            text = s.summary + ("\n\nLessons:\n" + "\n".join(f"- {l}" for l in s.lessons) if s.lessons else "")
            text += f"\n\nThe numbers:\n{s.facts}"
            items.append((s.end, s.kind, f"{'Monthly' if s.kind == 'month' else 'Weekly'} summary: {s.label}", text))
        order = {"month": 0, "week": 1, "day": 2}  # on a shared date, the bigger summary leads
        return sorted(items, key=lambda i: (i[0], -order[i[1]]), reverse=True)
