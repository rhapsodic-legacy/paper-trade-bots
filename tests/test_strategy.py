"""Rule language, indicators, backtester, history import and the strategy lab (with a scripted fake Claude)."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from papertrade import Ledger
from papertrade.brain import ClaudeBrain
from papertrade.history import HistoryStore, normalize_symbol, read_csv
from papertrade.lab import NOTEBOOK_SCHEMA, PROPOSAL_SCHEMA, StrategyLab, active_system, system_notes
from papertrade.personas import BY_NAME
from papertrade.strategy import Market, SpecError, backtest, indicator, parse_system, signals

D0 = date(2020, 1, 1)


def bars(prices, start=D0, volume=1000.0):
    return [(start + timedelta(days=i), float(p), volume) for i, p in enumerate(prices)]


def system(**over):
    raw = {"name": "T", "idea": "i", "symbols": ["AAA"], "direction": "long",
           "entry": [{"left": "price", "op": ">", "right": "100"}], "exit": [],
           "position_pct": 100, "max_positions": 1, "stop_loss_pct": 0, "take_profit_pct": 0, "max_hold_days": 0}
    raw.update(over)
    return raw


class IndicatorTest(unittest.TestCase):
    def test_values(self):
        c = [1, 2, 3, 4, 5, 6]
        self.assertEqual(indicator("sma", 3, c, [None] * 6), [None, None, 2, 3, 4, 5])
        self.assertEqual(indicator("high", 2, c, [None] * 6), [None, None, 2, 3, 4, 5])  # excludes today
        self.assertEqual(indicator("return", 1, [100, 110], [None] * 2)[1], 10.000000000000009)
        self.assertEqual(indicator("rsi", 3, c, [None] * 6)[-1], 100.0)  # only gains
        self.assertEqual(indicator("volume_ratio", 2, c, [None] * 6), [None] * 6)  # no volume data
        self.assertAlmostEqual(indicator("drawdown", 3, [10, 8, 9], [None] * 3)[-1], -10.0)


class ParseTest(unittest.TestCase):
    def test_rejections_explain_themselves(self):
        cases = [(system(symbols=["ZZZ"]), "aren't on your list"),
                 (system(direction="short"), "doesn't short"),
                 (system(entry=[]), "at least one"),
                 (system(entry=[{"left": "sma:500", "op": ">", "right": "1"}]), "between 2 and 250"),
                 (system(entry=[{"left": "1", "op": ">", "right": "2"}]), "at least one indicator"),
                 (system(position_pct=0), "position_pct")]
        for raw, msg in cases:
            with self.assertRaisesRegex(SpecError, msg):
                parse_system(raw, {"AAA"}, {"AAA"}, can_short=False)


class BacktestTest(unittest.TestCase):
    def test_fills_next_close_never_same_day(self):
        prices = [90, 90, 101, 120, 120]  # signal on day 2's close; fill must be day 3 at 120, not 101
        m = Market({"AAA": bars(prices)})
        r = backtest(parse_system(system(), {"AAA"}, {"AAA"}, False), m, D0, D0 + timedelta(days=4), fee=0)
        self.assertAlmostEqual(r["metrics"]["total_return_pct"], 0.0)
        self.assertEqual(r["open_positions"], 1)

    def test_profit_exit_and_fees(self):
        prices = [101, 110, 121, 121]
        raw = system(exit=[{"left": "price", "op": ">", "right": "115"}])
        m = Market({"AAA": bars(prices)})
        r = backtest(parse_system(raw, {"AAA"}, {"AAA"}, False), m, D0, D0 + timedelta(days=3), fee=0)
        self.assertEqual(r["trades"][0]["entry"], 110.0)   # entered day 1
        self.assertEqual(r["trades"][0]["exit"], 121.0)    # exit signal day 2, filled day 3
        self.assertAlmostEqual(r["metrics"]["total_return_pct"], 10.0)
        with_fees = backtest(parse_system(raw, {"AAA"}, {"AAA"}, False), m, D0, D0 + timedelta(days=3), fee=0.01)
        self.assertLess(with_fees["metrics"]["total_return_pct"], 10.0)

    def test_short_profits_when_price_falls(self):
        prices = [101, 100, 80, 80]
        raw = system(direction="short", position_pct=50)
        m = Market({"AAA": bars(prices)})
        r = backtest(parse_system(raw, {"AAA"}, {"AAA"}, True), m, D0, D0 + timedelta(days=3), fee=0)
        self.assertAlmostEqual(r["metrics"]["total_return_pct"], 10.0)  # half the account, price -20%

    def test_stop_loss_and_benchmark(self):
        prices = [101, 100, 85, 70, 60]
        m = Market({"AAA": bars(prices)})
        r = backtest(parse_system(system(stop_loss_pct=10), {"AAA"}, {"AAA"}, False), m, D0, D0 + timedelta(days=4), fee=0)
        self.assertEqual(r["trades"][0]["exit"], 70.0)  # stop hit at 85's close, out at next close
        self.assertAlmostEqual(r["benchmark"]["total_return_pct"], (60 / 101 - 1) * 100, places=1)

    def test_cross_market_reference_and_signals(self):
        m = Market({"AAA": bars([101] * 5), "SPY": bars([10, 10, 10, 10, 20])})
        raw = system(entry=[{"left": "SPY@price", "op": "crosses_above", "right": "15"}])
        s = parse_system(raw, {"AAA", "SPY"}, {"AAA"}, False)
        self.assertEqual(signals(s, m, D0 + timedelta(days=3), set())[0][:13], "AAA: no entry")
        self.assertIn("ENTRY signal", signals(s, m, D0 + timedelta(days=4), set())[0])


class HistoryTest(unittest.TestCase):
    def write(self, name, text):
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_formats(self):
        self.assertEqual([normalize_symbol(x) for x in ("aapl.us", "BTC-USD", "coin_Ethereum")], ["AAPL", "BTC", "ETH"])
        per_file = self.write("aapl.us.txt", "Date,Open,High,Low,Close,Volume,OpenInt\n2017-01-03,1,1,1,115.8,100,0\n")
        rows, _ = read_csv(per_file)
        self.assertEqual(rows[0][:2], ("AAPL", date(2017, 1, 3)))
        long_fmt = self.write("all_stocks_5yr.csv", "date,open,high,low,close,volume,Name\n2013-02-08,1,1,1,14.75,8407500,AAL\n")
        self.assertEqual(read_csv(long_fmt)[0][0][0], "AAL")
        coin = self.write("coin_Bitcoin.csv", "SNo,Name,Symbol,Date,High,Low,Open,Close,Volume,Marketcap\n"
                                               "1,Bitcoin,BTC,2013-04-29 23:59:59,1,1,1,144.54,0,0\n")
        self.assertEqual(read_csv(coin)[0][0][:2], ("BTC", date(2013, 4, 29)))
        yahoo = self.write("SPY.csv", "Date,Open,High,Low,Close,Adj Close,Volume\n2020-01-02,1,1,1,324.87,300.29,1\nbad,1,1,1,x,y,1\n")
        rows, warnings = read_csv(yahoo)
        self.assertEqual(str(rows[0][2]), "300.29")  # adjusted close preferred
        self.assertEqual(len(warnings), 1)

    def test_import_filters_to_universe(self):
        store = HistoryStore(":memory:")
        path = self.write("all.csv", "date,close,Name\n2020-01-02,10,AAPL\n2020-01-02,5,ZZZZ\n")
        counts, _ = store.import_csv(path, only={"AAPL"})
        self.assertEqual(counts, {"AAPL": 1})


class FakeClaude:
    def __init__(self, proposals):
        self.proposals, self.requests = list(proposals), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        schema = kw["output_config"]["format"]["schema"]
        body = ({"reasoning": "r", "system": self.proposals.pop(0)} if schema is PROPOSAL_SCHEMA
                else {"notebook": "Looked great in training, less so after.", "trust_level": 2})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class LabTest(unittest.TestCase):
    def market(self):
        import math
        days = 1500
        prices = [100 * (1.0004 ** i) * (1 + 0.05 * math.sin(i / 15)) for i in range(days)]
        return Market({"NVDA": bars(prices), "SPY": bars(prices), "BTC": bars(prices)})

    def test_build_refine_and_test_once(self):
        trend = system(symbols=["NVDA"], entry=[{"left": "price", "op": ">", "right": "sma:50"}],
                       exit=[{"left": "price", "op": "crosses_below", "right": "sma:50"}])
        broken = system(symbols=["BTC"])  # not on the Momentum Trader's list
        client = FakeClaude([trend, broken, trend])
        ledger = Ledger()
        lab = StrategyLab(ledger.db, self.market())
        result = lab.build(BY_NAME["Momentum Trader"], ClaudeBrain(client), rounds=3, log=lambda *_: None)
        self.assertEqual(len(client.requests), 4)  # 3 rounds + notebook
        self.assertIn("REJECTED", result.rounds[1]["result"])
        self.assertIn("Attempt 2", client.requests[2]["messages"][0]["content"])  # feedback reaches the next round
        # The bot never sees test-period dates while designing.
        test_start = result.test["metrics"]["start"]
        for r in client.requests[:3]:
            self.assertNotIn(test_start, r["messages"][0]["content"])
        self.assertIn("never seen before", client.requests[3]["messages"][0]["content"])
        saved = active_system(ledger.db, "Momentum Trader")
        self.assertEqual(saved["spec"]["name"], "T")
        self.assertEqual(saved["trust"], 2)

    def test_signals_reach_the_briefing(self):
        from papertrade import Account, HistoricalPriceFeed
        from papertrade.sim import MarketView
        ledger = Ledger()
        lab = StrategyLab(ledger.db, self.market())
        trend = system(symbols=["NVDA"], entry=[{"left": "price", "op": ">", "right": "sma:50"}])
        lab.build(BY_NAME["Momentum Trader"], ClaudeBrain(FakeClaude([trend])), rounds=1, log=lambda *_: None)
        m = self.market()
        feed = HistoricalPriceFeed((d, "NVDA", str(round(c, 2))) for d, c, _ in m.data["NVDA"])
        day = m.data["NVDA"][-1][0]
        view = MarketView(day, feed.prices(day), ("NVDA",), {}, Account("Momentum Trader"), feed)
        notes = system_notes(ledger.db, BY_NAME["Momentum Trader"], view)
        self.assertIn("On unseen test years", notes)
        self.assertRegex(notes, r"NVDA: (ENTRY signal|no entry)")

    def test_short_history_is_refused(self):
        lab = StrategyLab(Ledger().db, Market({"NVDA": bars([100] * 100)}))
        with self.assertRaisesRegex(SpecError, "year and a half"):
            lab.build(BY_NAME["Momentum Trader"], ClaudeBrain(FakeClaude([])), log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
