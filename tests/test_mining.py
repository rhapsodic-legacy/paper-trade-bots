"""Journal mining: round trips are built in code, Claude only labels the reasons and draws lessons."""
import unittest
from datetime import date
from decimal import Decimal as D

from papertrade import HistoricalPriceFeed, Ledger, Order, Side, Simulation
from papertrade.journal import JournalMemory
from papertrade.mining import JournalMiner, lessons_for, round_trips
from papertrade.universe import ASSETS

OCT = [date(2026, 10, d) for d in (5, 6, 7, 8)]
NOV2 = date(2026, 11, 2)


class FakeBrain:
    def __init__(self, answer):
        self.answer, self.messages = answer, []

    def ask(self, system, message, schema):
        self.messages.append(message)
        return self.answer, "" if self.answer else "offline", {"input_tokens": 10, "output_tokens": 5}


def make_ledger():
    rows = [(d, "AAPL", p) for d, p in zip(OCT, ("100", "110", "120", "90"))]
    rows += [(d, "SPY", p) for d, p in zip(OCT, ("500", "490", "480", "470"))]
    rows += [(d, "GME", p) for d, p in zip(OCT, ("20", "18", "15", "12"))]
    sim = Simulation(ASSETS, HistoricalPriceFeed(rows), Ledger())
    plan = {
        "Priya": [[Order("AAPL", Side.BUY, D("10"), "trend is up", "if it closes under 95")],
                  [], [Order("AAPL", Side.SELL, D("10"), "taking profit")], []],
        "Carl": [[Order("SPY", Side.SHORT, D("10"), "everyone's too happy", "if SPY makes a new high")],
                 [Order("SPY", Side.COVER, D("5"), "half off")], [], []],
        "Tyler": [[Order("GME", Side.BUY, D("100"), "vibes")], [], [], []],  # still open, losing
    }
    for i, day in enumerate(OCT):
        sim.step(day, {n: (lambda v, p=p, i=i: p[i]) for n, p in plan.items()})
    return sim.ledger


ANSWER = {"labels": [{"trip": 1, "category": "trend or momentum"}, {"trip": 2, "category": "dip-buying or contrarian"},
                     {"trip": 3, "category": "dip-buying or contrarian"}, {"trip": 4, "category": "gut feel or hype"},
                     {"trip": 99, "category": "macro view"}],
          "patterns": ["Momentum worked once; one month is a tiny sample."],
          "by_bot": [{"bot": "Tyler", "lessons": ["Write down why before you buy.", "Set a stop."]}],
          "summary": "Two small wins, one open loser."}


class MiningTest(unittest.TestCase):
    def test_round_trips_from_the_ledger(self):
        ledger = make_ledger()
        from papertrade.standing import StandingOrders
        theses = StandingOrders(ledger.db)
        priya = round_trips(ledger, "Priya", OCT[0], OCT[-1], theses.theses("Priya"))
        self.assertEqual([(t.symbol, t.direction, t.pnl, t.pct) for t in priya], [("AAPL", "long", D("200"), D("20"))])
        self.assertEqual(priya[0].invalidation, "if it closes under 95")
        carl = round_trips(ledger, "Carl", OCT[0], OCT[-1])
        self.assertEqual([(t.closed, t.pnl) for t in carl], [(OCT[1], D("50")), (None, D("150"))])
        tyler = round_trips(ledger, "Tyler", OCT[0], OCT[-1])
        self.assertEqual((tyler[0].closed, tyler[0].pnl, tyler[0].won), (None, D("-800"), False))

    def test_mine_once_per_finished_month_and_feed_back(self):
        ledger = make_ledger()
        miner, brain = JournalMiner(ledger), FakeBrain(ANSWER)
        self.assertEqual(miner.mine(brain, date(2026, 10, 20)), [])  # October isn't over yet
        [report] = miner.mine(brain, NOV2)
        self.assertIn("Why opened: 05 Oct: trend is up", brain.messages[0])
        self.assertIn("Said it would be wrong if: if it closes under 95", brain.messages[0])
        by_cat = {s["category"]: s for s in report["stats"]}
        self.assertEqual(by_cat["trend or momentum"], {"category": "trend or momentum", "trips": 1, "wins": 1,
                                                       "total_pnl": 200.0, "avg_pct": 20.0})
        self.assertEqual(by_cat["gut feel or hype"]["total_pnl"], -800.0)
        self.assertNotIn("macro view", by_cat)  # Claude's label for a trip that doesn't exist is ignored
        self.assertEqual(miner.mine(brain, NOV2), [])  # done once
        self.assertEqual(len(brain.messages), 1)
        recall = JournalMemory(ledger).recall("Tyler", NOV2)
        self.assertIn("gut feel or hype: 0 of 1 won, $-800 total", recall)
        self.assertIn("- Set a stop.", recall)
        self.assertNotIn("lessons for you", lessons_for(ledger.db, "Priya"))

    def test_offline_month_is_retried(self):
        ledger = make_ledger()
        miner = JournalMiner(ledger)
        [report] = miner.mine(FakeBrain(None), NOV2)
        self.assertEqual(report["stats"], [])
        self.assertEqual(lessons_for(ledger.db, "Tyler"), "")
        self.assertEqual(len(miner.mine(FakeBrain(ANSWER), NOV2)), 1)

    def test_published_as_markdown(self):
        import tempfile
        from pathlib import Path
        import publish_results
        path = Path(tempfile.mkdtemp()) / "l.db"
        ledger = make_ledger()
        ledger.db.execute("VACUUM INTO ?", (str(path),))
        JournalMiner(Ledger(str(path))).mine(FakeBrain(ANSWER), NOV2)
        root = Path(tempfile.mkdtemp())
        publish_results.write(str(path), root)
        text = (root / "results" / "lessons" / "2026-10.md").read_text()
        self.assertIn("| trend or momentum | 1 | 1 | $200 | +20.0% |", text)
        self.assertIn("| Tyler | long GME | 2026-10-05 | still open | -$800 (-40.0%) | gut feel or hype |", text)


if __name__ == "__main__":
    unittest.main()
