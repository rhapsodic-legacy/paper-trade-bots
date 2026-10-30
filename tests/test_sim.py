import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D

from papertrade import (Account, Asset, AssetClass, Broker, HistoricalPriceFeed, Ledger, Order,
                        OrderRejected, Side, Simulation)

MON, SAT = date(2026, 10, 19), date(2026, 10, 24)
ASSETS = {"AAPL": Asset("AAPL", AssetClass.STOCK), "BTC": Asset("BTC", AssetClass.CRYPTO)}


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.acct = Account("t")
        self.broker = Broker(ASSETS)
        self.prices = {"AAPL": D("100"), "BTC": D("50000")}

    def fill(self, symbol, side, qty, day=MON, prices=None):
        return self.broker.execute(self.acct, Order(symbol, side, D(qty)), day, prices or self.prices)

    def test_buy_then_sell_at_profit(self):
        self.fill("AAPL", Side.BUY, "10")
        self.assertEqual(self.acct.cash, D("99000"))
        trade = self.fill("AAPL", Side.SELL, "10", prices={**self.prices, "AAPL": D("110")})
        self.assertEqual(trade.realized_pnl, D("100.00"))
        self.assertEqual(self.acct.cash, D("100100"))
        self.assertNotIn("AAPL", self.acct.positions)

    def test_average_price_blends(self):
        self.fill("AAPL", Side.BUY, "10")
        self.fill("AAPL", Side.BUY, "10", prices={**self.prices, "AAPL": D("120")})
        self.assertEqual(self.acct.position("AAPL").avg_price, D("110"))

    def test_cannot_overspend(self):
        with self.assertRaisesRegex(OrderRejected, "Not enough cash"):
            self.fill("AAPL", Side.BUY, "1001")

    def test_cannot_sell_more_than_held(self):
        self.fill("AAPL", Side.BUY, "5")
        with self.assertRaises(OrderRejected):
            self.fill("AAPL", Side.SELL, "6")

    def test_stocks_closed_on_weekend_crypto_open(self):
        with self.assertRaisesRegex(OrderRejected, "closed"):
            self.fill("AAPL", Side.BUY, "1", day=SAT)
        self.fill("BTC", Side.BUY, "0.5", day=SAT)
        self.assertEqual(self.acct.position("BTC").quantity, D("0.5"))

    def test_stocks_whole_shares_crypto_fractional(self):
        self.assertEqual(self.fill("AAPL", Side.BUY, "2.7").quantity, D("2"))
        self.assertEqual(self.fill("BTC", Side.BUY, "0.123456789").quantity, D("0.12345678"))

    def test_short_and_cover(self):
        self.fill("AAPL", Side.SHORT, "100")
        self.assertEqual(self.acct.cash, D("110000"))
        falling = {**self.prices, "AAPL": D("90")}
        self.assertEqual(self.acct.equity(falling), D("101000.00"))
        trade = self.fill("AAPL", Side.COVER, "100", prices=falling)
        self.assertEqual(trade.realized_pnl, D("1000.00"))
        self.assertEqual(self.acct.cash, D("101000"))

    def test_short_margin_limit(self):
        # Equity 100k supports up to 200k of short exposure at 50% margin.
        self.fill("AAPL", Side.SHORT, "2000")
        with self.assertRaisesRegex(OrderRejected, "margin"):
            self.fill("AAPL", Side.SHORT, "1")

    def test_cannot_mix_long_and_short(self):
        self.fill("AAPL", Side.BUY, "1")
        with self.assertRaises(OrderRejected):
            self.fill("AAPL", Side.SHORT, "1")

    def test_fees_reduce_cash_and_pnl(self):
        broker = Broker(ASSETS, fee_rate=D("0.001"))
        trade = broker.execute(self.acct, Order("AAPL", Side.BUY, D("10")), MON, self.prices)
        self.assertEqual(trade.fee, D("1.00"))
        self.assertEqual(self.acct.equity(self.prices), D("99999.00"))


class SimulationTest(unittest.TestCase):
    def feed(self):
        return HistoricalPriceFeed([
            (MON, "AAPL", "100"), (date(2026, 10, 20), "AAPL", "105"),
            (MON, "BTC", "50000"), (date(2026, 10, 20), "BTC", "45000"),
        ])

    def test_history_and_weekend_carry_forward(self):
        feed = self.feed()
        self.assertEqual(feed.prices(SAT)["AAPL"], D("105"))
        self.assertEqual(feed.history("AAPL", SAT, 5), [D("100"), D("105")])

    def test_step_leaderboard_and_resume(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.db")
        sim = Simulation(list(ASSETS.values()), self.feed(), Ledger(path))
        bots = {
            "Stocky": lambda v: [Order("AAPL", Side.BUY, D("100"), "like apples")] if v.day == MON else [],
            "Bob": lambda v: [Order("BTC", Side.BUY, D("1"))] if v.day == MON else [],
        }
        sim.step(MON, bots)
        result = sim.step(date(2026, 10, 20), bots)
        self.assertEqual(result.rejections, [])
        board = sim.leaderboard(date(2026, 10, 20))
        self.assertEqual([r[0] for r in board], ["Stocky", "Bob"])
        self.assertEqual(board[0][2], D("0.50"))
        self.assertEqual(board[1][2], D("-5.00"))

        resumed = Simulation(list(ASSETS.values()), self.feed(), Ledger(path))
        self.assertEqual(resumed.accounts["Stocky"].position("AAPL").quantity, D("100"))
        self.assertEqual(resumed.ledger.trades("Stocky")[0].reason, "like apples")
        self.assertEqual(len(resumed.ledger.snapshots("Bob")), 2)

    def test_rejections_are_reported_not_raised(self):
        sim = Simulation(list(ASSETS.values()), self.feed())
        result = sim.step(MON, {"Greedy": lambda v: [Order("AAPL", Side.BUY, D("1000000"))]})
        self.assertEqual(len(result.rejections), 1)
        self.assertIn("Not enough cash", result.rejections[0][2])


if __name__ == "__main__":
    unittest.main()
