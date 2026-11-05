"""Standing orders (stops, targets, limits), trade theses and the S&P 500 benchmark."""
import unittest
from datetime import date
from decimal import Decimal as D

from papertrade import HistoricalPriceFeed, Ledger, Order, Side, Simulation
from papertrade.brain import ClaudeBrain, PersonaBot
from papertrade.briefing import build_briefing
from papertrade.personas import BY_NAME
from papertrade.standing import StandingOrder
from papertrade.universe import ASSETS
from tests.test_brain import FakeClient, answer

DAYS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6)]  # Monday to Friday


def make_sim(aapl=("200", "190", "180", "210", "230"), spy=("500", "505", "510", "515", "520")):
    rows = [(d, "AAPL", a) for d, a in zip(DAYS, aapl)] + [(d, "SPY", p) for d, p in zip(DAYS, spy)]
    rows += [(d, "BTC", "50000") for d in DAYS]
    return Simulation(ASSETS, HistoricalPriceFeed(rows), Ledger())


def script(*days):
    """A strategy that plays back one list of orders per day."""
    plan = list(days)
    return lambda view: plan.pop(0) if plan else []


class StandingOrderTest(unittest.TestCase):
    def test_stop_loss_fires_at_the_close_that_crosses_it(self):
        sim = make_sim()
        bot = script([Order("AAPL", Side.BUY, D("10"), "growth", "if it loses $185"),
                      StandingOrder("AAPL", "stop_loss", D("185"), reason="protect")])
        sim.step(DAYS[0], {"Bot": bot})
        sim.step(DAYS[1], {"Bot": bot})  # 190: above the stop
        self.assertEqual(sim.accounts["Bot"].position("AAPL").quantity, D("10"))
        result = sim.step(DAYS[2], {"Bot": bot})  # 180: through the stop, fills at the close
        self.assertEqual([(t.side, t.quantity, t.price) for t in result.triggered], [("sell", D("10"), D("180"))])
        self.assertIn("stop-loss at $185", result.triggered[0].reason)
        self.assertNotIn("AAPL", sim.accounts["Bot"].positions)
        self.assertEqual(sim.standing.open_orders("Bot"), [])
        self.assertIn("fired", sim.standing.notes(sim.accounts["Bot"], DAYS[2]))

    def test_take_profit_and_short_stop(self):
        sim = make_sim()
        bot = script([Order("AAPL", Side.BUY, D("5"), "x"), StandingOrder("AAPL", "take_profit", D("205"))])
        carl = script([Order("SPY", Side.SHORT, D("10"), "x"), StandingOrder("SPY", "stop_loss", D("512"))])
        for day in DAYS:
            sim.step(day, {"Bot": bot, "Carl": carl})
        self.assertNotIn("AAPL", sim.accounts["Bot"].positions)   # sold at 210 on Thursday
        self.assertEqual(sim.ledger.trades("Bot")[-1].price, D("210"))
        self.assertNotIn("SPY", sim.accounts["Carl"].positions)   # covered at 515 on Thursday
        self.assertEqual(sim.ledger.trades("Carl")[-1].side, "cover")

    def test_limit_buy_waits_for_its_price_then_expires(self):
        sim = make_sim()
        bot = script([StandingOrder("AAPL", "limit_buy", D("185"), D("1800"), "usd", "buy the dip", 10),
                      StandingOrder("SPY", "limit_buy", D("400"), D("5"), "units", "deep dip", 1)])
        sim.step(DAYS[0], {"Bot": bot})
        sim.step(DAYS[1], {"Bot": bot})
        result = sim.step(DAYS[2], {"Bot": bot})  # AAPL closes at 180
        self.assertEqual([(t.symbol, t.quantity) for t in result.triggered], [("AAPL", D("10"))])
        self.assertEqual(sim.standing.open_orders("Bot"), [])  # the SPY one expired after a day
        self.assertIn("expired", sim.standing.notes(sim.accounts["Bot"], DAYS[2]))

    def test_placing_replaces_and_price_zero_cancels(self):
        sim = make_sim()
        bot = script([Order("AAPL", Side.BUY, D("5"), "x"), StandingOrder("AAPL", "stop_loss", D("150"))],
                     [StandingOrder("AAPL", "stop_loss", D("170"))],
                     [StandingOrder("AAPL", "stop_loss", D("0"))])
        sim.step(DAYS[0], {"Bot": bot})
        sim.step(DAYS[1], {"Bot": bot})
        self.assertEqual([o.price for o in sim.standing.open_orders("Bot")], [D("170")])
        sim.step(DAYS[2], {"Bot": bot})
        self.assertEqual(sim.standing.open_orders("Bot"), [])

    def test_bad_standing_orders_are_explained(self):
        sim = make_sim()
        bot = script([StandingOrder("AAPL", "stop_loss", D("150")),          # nothing to protect
                      Order("SPY", Side.BUY, D("1"), "x"),
                      StandingOrder("SPY", "stop_loss", D("600")),           # above the close
                      StandingOrder("AAPL", "limit_buy", D("250"), D("1"), "units")])  # above the close
        result = sim.step(DAYS[0], {"Bot": bot})
        problems = [m for _, ok, m in result.standing if not ok]
        self.assertEqual(len(problems), 3)
        self.assertIn("no position", problems[0])
        self.assertIn("trade now instead", problems[1])
        self.assertIn("just buy", problems[2])

    def test_thesis_and_benchmark_reach_the_briefing(self):
        sim = make_sim()
        bot = script([Order("AAPL", Side.BUY, D("5"), "x", "if iPhone sales disappoint")])
        sim.step(DAYS[0], {"Bot": bot})
        view = sim.view(DAYS[2], sim.accounts["Bot"])
        text = build_briefing(view, ["AAPL", "SPY"]).text
        self.assertIn("you'd be wrong if: if iPhone sales disappoint", text)
        self.assertIn("S&P 500 (SPY)", text)
        self.assertIn("+2.00%", text)  # SPY 500 -> 510
        self.assertIn("Standing orders: none", text)

    def test_claude_places_standing_orders_and_invalidations(self):
        sim = make_sim()
        client = FakeClient(answer(("AAPL", "buy", 2000, "usd", "momentum"),
                                   standing=[("AAPL", "stop_loss", 185, 0, "all", 0, "cut losers"),
                                             ("AAPL", "limit_short", 250, 10, "units", 5, "no shorting")]))
        bot = PersonaBot(BY_NAME["Momentum Trader"], ClaudeBrain(client))
        result = sim.step(DAYS[0], {"Momentum Trader": bot})
        self.assertEqual([o.describe() for o in sim.standing.open_orders("Momentum Trader")],
                         ["stop-loss on AAPL at $185 for the whole position"])
        self.assertTrue(any("doesn't short" in p for p in bot.feedback))
        self.assertIn("if it falls 10%", str(sim.standing.theses("Momentum Trader")))
        self.assertTrue(result.standing[0][1])

    def test_stops_fire_on_days_the_bot_is_not_called(self):
        sim = make_sim(aapl=("200", "190", "180", "210", "230"))
        sim.step(DAYS[0], {"Bot": script([Order("AAPL", Side.BUY, D("5"), "x"),
                                          StandingOrder("AAPL", "stop_loss", D("185"))])})
        result = sim.step(DAYS[2], {})  # nobody decides, the stop still fires
        self.assertEqual(len(result.triggered), 1)


if __name__ == "__main__":
    unittest.main()
