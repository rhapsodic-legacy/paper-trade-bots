"""The filings clerk: what the watched companies told the SEC, and what their insiders traded.

Uses SEC EDGAR, which is free and keyless but asks every script to say who it is in its
User-Agent (set PAPERTRADE_SEC_CONTACT to your name and email) and to stay under 10 requests a
second. Per daily fetch it makes one request per stock for its recent filings, plus one per new
insider-trade form (Form 4) to read whether it was a buy or a sale. Everything is cached in data.db,
so a filing is never downloaded twice and the bots only ever read the cache.

Flagged: 8-K (material events, with their item codes in plain words), 10-Q and 10-K (quarterly and
annual reports), Form 4 (insider trades), 144 (an insider's notice of a planned sale), and 13D/13G
(someone took a 5%+ stake).
"""
import json
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

from .http import fetch as http_fetch

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
FUNDS = {"SPY"}  # ETFs: their filings say nothing useful about the market

FORMS = {"8-K": "material event", "10-Q": "quarterly report", "10-K": "annual report",
         "4": "insider trade", "144": "planned insider sale", "SC 13D": "5%+ stake, may push for change",
         "SC 13G": "5%+ stake, passive", "SCHEDULE 13D": "5%+ stake, may push for change",
         "SCHEDULE 13G": "5%+ stake, passive"}
ITEMS_8K = {"1.01": "signed a major agreement", "1.02": "ended a major agreement", "1.05": "cybersecurity incident",
            "2.01": "completed an acquisition or sale", "2.02": "quarterly results",
            "2.03": "took on major debt", "2.05": "restructuring or layoffs", "2.06": "asset write-down",
            "3.01": "listing problem", "4.02": "past financials no longer reliable",
            "5.02": "executive or director change", "5.07": "shareholder vote results",
            "7.01": "investor presentation", "8.01": "other news"}
CODES = {"P": "bought", "S": "sold", "A": "was granted", "M": "exercised options for", "F": "gave up for tax",
         "G": "gifted", "D": "returned", "C": "converted"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_ciks (symbol TEXT PRIMARY KEY, cik INTEGER NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    filed TEXT NOT NULL,
    form TEXT NOT NULL,
    description TEXT NOT NULL,
    link TEXT NOT NULL,
    insider TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS filings_by_symbol ON filings (symbol, filed);
"""


@dataclass(frozen=True)
class Filing:
    filed: date
    symbol: str
    form: str
    description: str
    link: str
    insider: str = ""  # for Form 4: who traded what, in words


def contact_headers() -> dict:
    who = os.environ.get("PAPERTRADE_SEC_CONTACT", "paper-trade-bots@users.noreply.github.com")
    return {"User-Agent": f"PaperTrade educational simulator {who}"}


def parse_form4(payload: bytes) -> str:
    """'Jane Doe (CFO) sold 12,000 shares at about $201.50 under a pre-set 10b5-1 plan' and the like."""
    root = ET.fromstring(payload)
    owner = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "An insider"
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    role = ""
    if rel is not None:
        role = rel.findtext("officerTitle") or ("director" if rel.findtext("isDirector") in ("1", "true") else "")
    totals: dict[str, list[Decimal]] = {}
    for t in root.iter("nonDerivativeTransaction"):
        code = t.findtext("transactionCoding/transactionCode") or ""
        try:
            shares = Decimal(t.findtext("transactionAmounts/transactionShares/value") or "0")
            price = Decimal(t.findtext("transactionAmounts/transactionPricePerShare/value") or "0")
        except InvalidOperation:
            continue
        s = totals.setdefault(code, [Decimal(0), Decimal(0)])
        s[0] += shares
        s[1] += shares * price
    if not totals:
        return f"{owner}{f' ({role})' if role else ''} filed an insider form with no share trades"
    parts = []
    for code, (shares, value) in sorted(totals.items(), key=lambda kv: -kv[1][1]):
        words = CODES.get(code, f"made a code-{code} transaction in")
        at = f" at about ${value / shares:,.2f}" if shares and value else ""
        parts.append(f"{words} {shares:,.0f} shares{at}")
    plan = " under a pre-set 10b5-1 trading plan" if (root.findtext(".//aff10b5One") or "").strip() in ("1", "true") \
        else ""
    return f"{owner}{f' ({role})' if role else ''} {', and '.join(parts)}{plan}"


class FilingsClerk:
    def __init__(self, path=":memory:", fetch: Callable[..., bytes] = http_fetch, pause: float = 0.15):
        self.db = sqlite3.connect(path) if isinstance(path, str) else path
        self.db.executescript(SCHEMA)
        self.fetch, self.pause = fetch, pause

    def _get(self, url: str) -> bytes:
        time.sleep(self.pause)  # well under the SEC's 10 requests a second
        return self.fetch(url, headers=contact_headers())

    def _cik(self, symbol: str) -> tuple[int, str] | None:
        row = self.db.execute("SELECT cik, name FROM sec_ciks WHERE symbol = ?", (symbol,)).fetchone()
        return (row[0], row[1]) if row else None

    def _load_ciks(self) -> None:
        mapping = json.loads(self._get(TICKERS_URL))
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO sec_ciks VALUES (?, ?, ?)",
                                [(v["ticker"].upper(), int(v["cik_str"]), v["title"]) for v in mapping.values()])

    def update(self, symbols: Iterable[str], today: date, days_back: int = 14) -> dict[str, str]:
        """Pull recent filings for each stock. Returns {symbol or url: error}."""
        errors: dict[str, str] = {}
        symbols = [s for s in symbols if s not in FUNDS]
        if any(self._cik(s) is None for s in symbols):
            try:
                self._load_ciks()
            except Exception as e:
                return {TICKERS_URL: str(e)}
        since = (today - timedelta(days=days_back)).isoformat()
        for symbol in symbols:
            found = self._cik(symbol)
            if found is None:
                errors[symbol] = "no SEC company number for this ticker"
                continue
            cik, _ = found
            try:
                recent = json.loads(self._get(SUBMISSIONS_URL.format(cik=cik)))["filings"]["recent"]
            except Exception as e:
                errors[symbol] = str(e)
                continue
            rows = zip(recent.get("accessionNumber", []), recent.get("filingDate", []), recent.get("form", []),
                       recent.get("items", [""] * len(recent.get("form", []))), recent.get("primaryDocument", []))
            for accession, filed, form, items, document in rows:
                if filed < since or form not in FORMS:
                    continue
                if self.db.execute("SELECT 1 FROM filings WHERE accession = ?", (accession,)).fetchone():
                    continue  # already have it: never fetch a filing twice
                folder = accession.replace("-", "")
                link = FILING_URL.format(cik=cik, accession=folder)
                description = FORMS[form]
                if form == "8-K" and items:
                    words = [ITEMS_8K[i] for i in str(items).split(",") if i.strip() in ITEMS_8K]
                    if words:
                        description = ", ".join(dict.fromkeys(words))
                insider = ""
                if form == "4" and document:
                    raw = document.split("/")[-1]  # primaryDocument points at the styled view; the XML sits beside it
                    try:
                        insider = parse_form4(self._get(ARCHIVE_URL.format(cik=cik, accession=folder, document=raw)))
                    except Exception as e:
                        insider = ""
                        errors[f"{symbol} Form 4 {accession}"] = str(e)
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO filings VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (accession, symbol, filed, form, description, link, insider))
        return errors

    def recent(self, day: date, symbols: Iterable[str], days_back: int = 7) -> list[Filing]:
        """Filings made on or before `day`, newest first. Never anything filed after `day`."""
        wanted = [s for s in symbols if s not in FUNDS]
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = self.db.execute(
            f"SELECT filed, symbol, form, description, link, insider FROM filings WHERE symbol IN ({marks})"
            " AND filed <= ? AND filed >= ? ORDER BY filed DESC, symbol",
            (*wanted, day.isoformat(), (day - timedelta(days=days_back)).isoformat()))
        return [Filing(date.fromisoformat(f), s, form, d, link, ins) for f, s, form, d, link, ins in rows]

    def notes_for(self, day: date, symbols: Iterable[str], days_back: int = 7) -> str:
        """Plain-text summary: insider trades grouped per stock, other filings one per line."""
        lines, insiders = [], {}
        for f in self.recent(day, symbols, days_back):
            if f.form == "4":
                insiders.setdefault(f.symbol, []).append(f)
                continue
            form = "Form 144" if f.form == "144" else f.form
            lines.append(f"- {f.filed:%d %b} {f.symbol} {form}: {f.description}")
        for symbol, forms in insiders.items():
            sold = sum(" sold " in f" {f.insider} " for f in forms)
            bought = sum(" bought " in f" {f.insider} " for f in forms)
            head = f"- {symbol}: {len(forms)} insider trade filing{'s' if len(forms) > 1 else ''}"
            tally = ", ".join(x for x in (f"{sold} with sales" if sold else "", f"{bought} with purchases" if bought
                                          else "") if x)
            lines.append(head + (f" ({tally})" if tally else ""))
            lines += [f"    {f.filed:%d %b}: {f.insider}" for f in forms[:3] if f.insider]
        return "\n".join(lines)
