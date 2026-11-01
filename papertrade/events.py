"""The market calendar: earnings dates, Fed meetings, inflation and jobs reports, cached in SQLite.

Bots used to learn that "Apple reports Monday" only if a headline happened to say so. This puts
the dates in front of them. All sources are free and need no key:

  earnings   Nasdaq's earnings calendar, one request per weekday ahead (default two weeks)
  FOMC       the Federal Reserve's meeting calendar page
  CPI, jobs  the Bureau of Labor Statistics release schedules

Government sites sometimes turn away scripts, so the published Fed and BLS schedules through 2027
are built in and the live pages only add to them. Like the news, everything is fetched once a day
by fetch_data.py into data.db and read by every bot from there.
"""
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from html import unescape
from typing import Callable, Iterable

from .http import fetch as http_fetch

NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
FED_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_SCHEDULES = {"CPI": "https://www.bls.gov/schedule/news_release/cpi.htm",
                 "Jobs": "https://www.bls.gov/schedule/news_release/empsit.htm"}
MACRO = "*"  # symbol for events that move the whole market

# Published schedules as of November 2026 (FOMC: second day, when the decision is announced).
KNOWN = [
    *[("FOMC", d, "Fed interest-rate decision (2:00 pm ET)") for d in (
        "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
        "2027-10-27", "2027-12-08")],
    *[("CPI", d, "Consumer price index (inflation) report, 8:30 am ET") for d in (
        "2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12",
        "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10")],
    *[("Jobs", d, "Jobs report (nonfarm payrolls, unemployment), 8:30 am ET") for d in (
        "2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07",
        "2027-06-04", "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03")],
]
TITLES = {kind: title for kind, _, title in KNOWN}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (day, kind, symbol)
);
"""

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}


@dataclass(frozen=True)
class Event:
    day: date
    kind: str
    symbol: str
    title: str


def _text(html: bytes) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace"))))


def parse_fomc(html: bytes) -> list[date]:
    """Decision days from the Fed's calendar page, read from its text: '2026 FOMC Meetings ... March 17-18* ...'."""
    text, days = _text(html), []
    parts = re.split(r"(\d{4}) FOMC Meetings", text)
    for year, body in zip(parts[1::2], parts[2::2]):
        for m in re.finditer(r"\b(January|February|March|April|May|June|July|August|September|October|November|"
                             r"December)(?:/(\w+))?\s+(\d{1,2})-(\d{1,2})\b", body):
            month = MONTHS.get((m.group(2) or m.group(1)).lower())
            if month is None:
                continue
            try:
                days.append(date(int(year), month, int(m.group(4))))
            except ValueError:
                continue
    return days


def parse_bls(html: bytes) -> list[date]:
    """Release dates from a BLS schedule table: 'Nov. 10, 2026' or 'May 12, 2027'."""
    out = []
    for mon, day, year in re.findall(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})\b", _text(html)):
        month = next((n for name, n in MONTHS.items() if name.startswith(mon.lower()[:3])), None)
        if month:
            try:
                out.append(date(int(year), month, int(day)))
            except ValueError:
                pass
    return out


def parse_earnings(payload: bytes, symbols: Iterable[str], day: date) -> list[Event]:
    rows = ((json.loads(payload) or {}).get("data") or {}).get("rows") or []
    wanted, out = set(symbols), []
    for r in rows:
        symbol = str(r.get("symbol", "")).upper()
        if symbol not in wanted:
            continue
        when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}.get(r.get("time"), "")
        forecast = f", analysts expect EPS {r['epsForecast']}" if r.get("epsForecast") else ""
        out.append(Event(day, "Earnings", symbol, f"{r.get('name') or symbol} reports earnings"
                                                  f"{' ' + when if when else ''}{forecast}"))
    return out


class MarketCalendar:
    def __init__(self, path=":memory:", fetch: Callable[[str], bytes] = http_fetch):
        self.db = sqlite3.connect(path) if isinstance(path, str) else path
        self.db.executescript(SCHEMA)
        self.fetch = fetch
        self._add(Event(date.fromisoformat(d), kind, MACRO, title) for kind, d, title in KNOWN)

    def _add(self, events: Iterable[Event], source: str = "built-in") -> int:
        before = self.db.total_changes
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?)",
                                [(e.day.isoformat(), e.kind, e.symbol, e.title, source) for e in events])
        return self.db.total_changes - before

    def update(self, stocks: Iterable[str], today: date, days_ahead: int = 14) -> dict[str, str]:
        """Refresh earnings dates for `stocks` and the macro schedules. Returns {url: error}."""
        stocks, errors = list(stocks), {}
        for i in range(days_ahead + 1):
            day = today + timedelta(days=i)
            if day.weekday() >= 5:
                continue
            url = NASDAQ_EARNINGS.format(day=day.isoformat())
            try:
                with self.db:  # a stale earnings date for that day is dropped if it moved
                    self.db.execute("DELETE FROM events WHERE day = ? AND kind = 'Earnings' AND source = 'Nasdaq'",
                                    (day.isoformat(),))
                self._add(parse_earnings(self.fetch(url), stocks, day), "Nasdaq")
            except Exception as e:
                errors[url] = str(e)
        try:
            self._add((Event(d, "FOMC", MACRO, TITLES["FOMC"]) for d in parse_fomc(self.fetch(FED_CALENDAR))
                       if d >= today), "Federal Reserve")
        except Exception as e:
            errors[FED_CALENDAR] = str(e)
        for kind, url in BLS_SCHEDULES.items():
            try:
                self._add((Event(d, kind, MACRO, TITLES[kind]) for d in parse_bls(self.fetch(url)) if d >= today),
                          "BLS")
            except Exception as e:
                errors[url] = str(e)
        return errors

    def upcoming(self, day: date, symbols: Iterable[str], days_ahead: int = 10) -> list[Event]:
        """Events from `day` to `days_ahead` later, for these symbols plus market-wide ones."""
        wanted = list(symbols) + [MACRO]
        marks = ",".join("?" * len(wanted))
        rows = self.db.execute(
            f"SELECT day, kind, symbol, title FROM events WHERE day >= ? AND day <= ? AND symbol IN ({marks})"
            " ORDER BY day, kind", (day.isoformat(), (day + timedelta(days=days_ahead)).isoformat(), *wanted))
        return [Event(date.fromisoformat(d), k, s, t) for d, k, s, t in rows]

    def notes_for(self, day: date, symbols: Iterable[str], days_ahead: int = 10) -> str:
        lines = []
        for e in self.upcoming(day, symbols, days_ahead):
            when = "TODAY" if e.day == day else "tomorrow" if e.day == day + timedelta(days=1) else f"{e.day:%a %d %b}"
            tag = "Market" if e.symbol == MACRO else e.symbol
            lines.append(f"- {when}: [{tag}] {e.title}")
        return "\n".join(lines)
