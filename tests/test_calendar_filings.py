"""Market calendar and SEC filings clerk, against small hand-written payloads in the sources' shapes.

They are test fixtures, not real data.
"""
import json
import unittest
from datetime import date

from papertrade.events import MACRO, MarketCalendar, parse_bls, parse_earnings, parse_fomc
from papertrade.filings import FilingsClerk, parse_form4

FED = b"""<html><h4><a>2027 FOMC Meetings</a></h4>
<div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">26-27</div>
<div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">16-17*</div>
<div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">30-1</div>
<h4><a>2026 FOMC Meetings</a></h4>
<div class="fomc-meeting__month"><strong>December</strong></div><div class="fomc-meeting__date">8-9*</div></html>"""

BLS = b"""<table><tr><th>Reference Month</th><th>Release Date</th></tr>
<tr><td>October 2026</td><td>Nov. 10, 2026</td><td>08:30 AM</td></tr>
<tr><td>April 2027</td><td>May 12, 2027</td><td>08:30 AM</td></tr></table>"""

NASDAQ = json.dumps({"data": {"rows": [
    {"symbol": "AAPL", "name": "Apple Inc.", "time": "time-after-hours", "epsForecast": "$1.85"},
    {"symbol": "ZZZZ", "name": "Not watched", "time": "time-pre-market", "epsForecast": "$0.10"}]}}).encode()

TICKERS = json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}).encode()
SUBMISSIONS = json.dumps({"filings": {"recent": {
    "accessionNumber": ["0000320193-26-000101", "0000320193-26-000100", "0000320193-26-000050", "0000320193-25-000001"],
    "filingDate": ["2026-11-02", "2026-10-31", "2026-10-30", "2025-01-01"],
    "form": ["4", "8-K", "S-8", "10-K"],
    "items": ["", "2.02,9.01", "", ""],
    "primaryDocument": ["xslF345X05/wk-form4_1.xml", "aapl-8k.htm", "s8.htm", "10k.htm"]}}}).encode()
FORM4 = b"""<ownershipDocument><aff10b5One>1</aff10b5One>
<reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>200</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>202</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


class CalendarTest(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(parse_fomc(FED), [date(2027, 1, 27), date(2027, 3, 17), date(2027, 5, 1), date(2026, 12, 9)])
        self.assertEqual(parse_bls(BLS), [date(2026, 11, 10), date(2027, 5, 12)])
        events = parse_earnings(NASDAQ, ["AAPL", "MSFT"], date(2026, 11, 3))
        self.assertEqual([(e.symbol, e.title) for e in events],
                         [("AAPL", "Apple Inc. reports earnings after the close, analysts expect EPS $1.85")])

    def test_update_and_notes(self):
        def fetch(url):
            if "nasdaq" in url:
                return NASDAQ if url.endswith("2026-11-03") else json.dumps({"data": {"rows": None}}).encode()
            if "federalreserve" in url:
                return FED
            raise OSError("403 Forbidden")  # BLS turns the script away: the built-in dates still apply
        cal = MarketCalendar(fetch=fetch)
        errors = cal.update(["AAPL"], date(2026, 11, 2), days_ahead=7)
        self.assertEqual(len(errors), 2)
        notes = cal.notes_for(date(2026, 11, 2), ["AAPL"], days_ahead=10)
        self.assertIn("tomorrow: [AAPL] Apple Inc. reports earnings after the close", notes)
        self.assertIn("Fri 06 Nov: [Market] Jobs report", notes)
        self.assertIn("Tue 10 Nov: [Market] Consumer price index", notes)
        self.assertNotIn("[Market] Fed", notes)  # next FOMC is in December
        self.assertTrue(any(e.symbol == MACRO and e.kind == "FOMC"
                            for e in cal.upcoming(date(2026, 12, 1), [], days_ahead=10)))

    def test_moved_earnings_date_is_dropped(self):
        answers = {"2026-11-03": NASDAQ}
        cal = MarketCalendar(fetch=lambda url: answers.get(url[-10:], b'{"data": {"rows": []}}')
                             if "nasdaq" in url else b"")
        cal.update(["AAPL"], date(2026, 11, 2), days_ahead=3)
        answers.clear()
        cal.update(["AAPL"], date(2026, 11, 2), days_ahead=3)
        self.assertEqual([e for e in cal.upcoming(date(2026, 11, 2), ["AAPL"]) if e.symbol == "AAPL"], [])


class FilingsTest(unittest.TestCase):
    def make(self):
        calls = []

        def fetch(url, headers=None):
            calls.append(url)
            self.assertIn("PaperTrade", headers["User-Agent"])
            if url.endswith("company_tickers.json"):
                return TICKERS
            if "submissions" in url:
                return SUBMISSIONS
            if url.endswith("wk-form4_1.xml"):
                return FORM4
            raise AssertionError(url)
        return FilingsClerk(fetch=fetch, pause=0), calls

    def test_form4_in_words(self):
        self.assertEqual(parse_form4(FORM4),
                         "Doe Jane (CFO) sold 2,000 shares at about $201.00 under a pre-set 10b5-1 trading plan")

    def test_update_caches_and_summarises(self):
        clerk, calls = self.make()
        self.assertEqual(clerk.update(["AAPL", "SPY"], date(2026, 11, 2)), {})
        self.assertEqual(len(calls), 3)  # tickers, AAPL submissions, one Form 4; SPY is a fund, skipped
        clerk.update(["AAPL"], date(2026, 11, 2))
        self.assertEqual(len(calls), 4)  # just the submissions list again: known filings aren't refetched
        notes = clerk.notes_for(date(2026, 11, 2), ["AAPL", "SPY"])
        self.assertIn("31 Oct AAPL 8-K: quarterly results", notes)
        self.assertIn("AAPL: 1 insider trade filing (1 with sales)", notes)
        self.assertIn("Doe Jane (CFO) sold 2,000 shares", notes)
        self.assertNotIn("S-8", notes)
        self.assertNotIn("annual report", notes)  # older than the window
        # No peeking: on 1 Nov the 2 Nov Form 4 hasn't been filed yet.
        self.assertNotIn("insider", clerk.notes_for(date(2026, 11, 1), ["AAPL"]))


class BriefingTest(unittest.TestCase):
    def test_bots_and_desks_see_the_calendar_and_filings(self):
        from papertrade import Ledger
        from papertrade.brain import ClaudeBrain, PersonaBot
        from papertrade.desk import ResearchDesk
        from papertrade.personas import BY_NAME
        from papertrade.universe import ASSETS
        from tests.test_brain import FakeClient, answer
        from tests.test_standing import DAYS, make_sim
        cal = MarketCalendar(fetch=lambda url: NASDAQ if url.endswith("2026-11-03") else b'{"data": {}}')
        cal.update(["AAPL"], DAYS[0], days_ahead=3)
        clerk, _ = FilingsTest.make(self)
        clerk.update(["AAPL"], DAYS[0])
        client = FakeClient(answer())
        bot = PersonaBot(BY_NAME["Tech Growth Bull"], ClaudeBrain(client), calendar=cal, filings=clerk)
        make_sim().step(DAYS[0], {"Tech Growth Bull": bot})
        text = client.requests[0]["messages"][0]["content"]
        self.assertIn("## Coming up (market calendar)\n- tomorrow: [AAPL] Apple Inc. reports earnings", text)
        self.assertIn("## SEC filings this past week", text)
        desk = ResearchDesk(Ledger(), None, assets=ASSETS, calendar=cal, filings=clerk)
        tech = next(d for d in desk.desks if "AAPL" in d.symbols)
        brief = desk.brief(tech, DAYS[0], [], lambda s, n: [])
        self.assertIn("reports earnings", brief)
        self.assertIn("quarterly results", brief)


if __name__ == "__main__":
    unittest.main()
