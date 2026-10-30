import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D

from papertrade import Ledger, Order, Side, Simulation, HistoricalPriceFeed
from papertrade.report import collect
from papertrade.universe import ASSETS
import dashboard

MON, TUE = date(2026, 10, 19), date(2026, 10, 20)


class DashboardTest(unittest.TestCase):
    def make_ledger(self):
        path = os.path.join(tempfile.mkdtemp(), "l.db")
        feed = HistoricalPriceFeed([(MON, "BTC", "50000"), (TUE, "BTC", "55000"), (MON, "SPY", "500"), (TUE, "SPY", "490")])
        sim = Simulation(ASSETS, feed, Ledger(path))
        bob = lambda v: [Order("BTC", Side.BUY, D("1"), "stack")] if v.day == MON else []
        carl = lambda v: [Order("SPY", Side.SHORT, D("10"), "too happy")] if v.day == MON else []
        sim.step(MON, {"Bitcoin Bob": bob, "Contrarian Carl": carl})
        sim.step(TUE, {"Bitcoin Bob": bob, "Contrarian Carl": carl})
        sim.ledger.record_journal(TUE, "Bitcoin Bob", "Sneaky </script><b>entry</b>", "Up")
        return path

    def test_collect(self):
        data = collect(Ledger(self.make_ledger()))
        self.assertEqual(data["days"], ["2026-10-19", "2026-10-20"])
        bob, carl = {b["name"]: b for b in data["bots"]}["Bitcoin Bob"], {b["name"]: b for b in data["bots"]}["Contrarian Carl"]
        self.assertEqual(bob["series"], [100000.0, 105000.0])
        self.assertEqual(bob["holdings"][0]["price"], 55000.0)  # valued at the latest marked price
        self.assertEqual(carl["holdings"][0]["side"], "short")
        self.assertEqual(carl["holdings"][0]["pnl"], 100.0)
        self.assertEqual(bob["market_view"], "Up")
        self.assertEqual(bob["slot"] < carl["slot"], False)  # colours follow persona order, not rank

    def test_page_escapes_journal_text(self):
        out = os.path.join(tempfile.mkdtemp(), "d.html")
        html = dashboard.build(self.make_ledger(), out, "Demo").read_text()
        self.assertNotIn("</script><b>", html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertEqual(html.count("</script>"), 2)


if __name__ == "__main__":
    unittest.main()


class PublishTest(unittest.TestCase):
    def test_write_results_without_git(self):
        import json
        from pathlib import Path
        import publish_results
        path = DashboardTest().make_ledger()
        root = Path(tempfile.mkdtemp())
        publish_results.write(path, root)
        latest = json.loads((root / "results" / "latest.json").read_text())
        self.assertEqual(latest["as_of"], "2026-10-20")
        self.assertTrue((root / "results" / "days" / "2026-10-20.json").exists())
        text = (root / "results" / "README.md").read_text()
        self.assertIn("| 1 | Bitcoin Bob | $105,000 | +5.00% |", text)
        self.assertIn("Sneaky", text)
        self.assertNotIn("__DATA__", (root / "docs" / "index.html").read_text())
        self.assertEqual(publish_results.push(root, "2026-10-20"), "not a git repo with a remote; skipped push")
