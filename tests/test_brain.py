"""Brain tests swap the Anthropic client for a fake that returns canned answers, so they run offline and free."""
import json
import unittest
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from papertrade import HistoricalPriceFeed, Ledger, Side, Simulation
from papertrade.brain import DECISION_SCHEMA, ClaudeBrain, PersonaBot, system_prompt
from papertrade.personas import BY_NAME, PERSONAS
from papertrade.universe import ASSETS

MON, SAT = date(2026, 10, 19), date(2026, 10, 24)


class FakeClient:
    """Stands in for anthropic.Anthropic(); records each request and replies with the next canned answer."""

    def __init__(self, *answers, stop_reason="end_turn", error=None):
        self.answers, self.stop_reason, self.error, self.requests = list(answers), stop_reason, error, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        answer = self.answers.pop(0) if self.answers else {"market_view": "", "orders": [], "journal_entry": "Quiet."}
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def answer(*orders, journal="Did a thing.", standing=()):
    return {"market_view": "Up, probably.", "journal_entry": journal,
            "orders": [dict(zip(("symbol", "side", "amount", "unit", "reason"), o), invalidation="if it falls 10%")
                       for o in orders],
            "standing_orders": [dict(zip(("symbol", "kind", "price", "amount", "unit", "expires_days", "reason"), o))
                                for o in standing]}


def make_sim():
    rows = [(d, s, p) for d in (MON, SAT) for s, p in
            (("AAPL", "200"), ("SPY", "500"), ("GME", "20"), ("KO", "70"), ("JNJ", "150"), ("MSFT", "400"),
             ("NVDA", "130"), ("BTC", "50000"), ("ETH", "2500"), ("SOL", "150"), ("DOGE", "0.15"))]
    return Simulation(ASSETS, HistoricalPriceFeed(rows), Ledger())


class PersonaTest(unittest.TestCase):
    def test_seven_personas_with_tradable_watchlists(self):
        symbols = {a.symbol: a for a in ASSETS}
        self.assertEqual(len(PERSONAS), 7)
        for p in PERSONAS:
            for s in p.watchlist:
                self.assertIn(symbols[s].asset_class, p.asset_classes, f"{p.name} watches {s}")
        self.assertTrue(BY_NAME["Contrarian Carl"].can_short)
        self.assertEqual({symbols[s].asset_class.value for s in BY_NAME["Bitcoin Bob"].watchlist}, {"crypto"})

    def test_system_prompt_mentions_character_and_rules(self):
        prompt = system_prompt(BY_NAME["Contrarian Carl"])
        self.assertIn("Carl", prompt)
        self.assertIn("allowed to short", prompt)
        self.assertIn("do not short", system_prompt(BY_NAME["Cautious Retiree"]))


class BrainTest(unittest.TestCase):
    def run_bot(self, name, client, day=MON):
        sim = make_sim()
        bot = PersonaBot(BY_NAME[name], ClaudeBrain(client))
        result = sim.step(day, {name: bot})
        return sim, bot, result

    def test_request_shape(self):
        client = FakeClient(answer())
        self.run_bot("Value Investor", client)
        req = client.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["output_config"]["format"]["schema"], DECISION_SCHEMA)
        self.assertEqual(req["output_config"]["effort"], "medium")
        self.assertEqual(req["fallbacks"], "default")
        self.assertIn("Briefing for Value Investor", req["messages"][0]["content"])

    def test_usd_and_units_orders_fill(self):
        client = FakeClient(answer(("KO", "buy", 7000, "usd", "cheap"), ("JNJ", "buy", 10, "units", "steady")))
        sim, bot, result = self.run_bot("Value Investor", client)
        self.assertEqual([(t.symbol, t.quantity) for t in result.fills], [("KO", D("100")), ("JNJ", D("10"))])
        self.assertEqual(result.fills[0].reason, "cheap")
        self.assertEqual(bot.last_decision.journal_entry, "Did a thing.")

    def test_guardrails_drop_bad_orders(self):
        client = FakeClient(answer(("AAPL", "buy", 1000, "usd", "x"),    # Bob doesn't trade stocks
                                   ("XYZ", "buy", 1000, "usd", "x"),     # unknown
                                   ("BTC", "short", 1, "units", "x"),    # Bob can't short
                                   ("BTC", "buy", 5000, "usd", "x")))
        sim, bot, result = self.run_bot("Bitcoin Bob", client)
        self.assertEqual([t.symbol for t in result.fills], ["BTC"])
        self.assertEqual(len(bot.feedback), 3)

    def test_stock_bots_skip_weekends_without_calling_claude(self):
        client = FakeClient(answer(("SPY", "buy", 1000, "usd", "x")))
        sim, bot, result = self.run_bot("Cautious Retiree", client, day=SAT)
        self.assertEqual((result.fills, client.requests, bot.last_decision), ([], [], None))

    def test_carl_shorts_and_covers_all(self):
        sim = make_sim()
        carl = PersonaBot(BY_NAME["Contrarian Carl"], ClaudeBrain(FakeClient(
            answer(("SPY", "short", 10000, "usd", "too happy")), answer(("SPY", "cover", 0, "all", "done")))))
        sim.step(MON, {"Contrarian Carl": carl})
        self.assertEqual(sim.accounts["Contrarian Carl"].position("SPY").quantity, D("-20"))
        sim.step(date(2026, 10, 20), {"Contrarian Carl": carl})
        self.assertNotIn("SPY", sim.accounts["Contrarian Carl"].positions)

    def test_broker_rejection_reaches_next_briefing(self):
        client = FakeClient(answer(("GME", "buy", 500000, "usd", "yolo")), answer())
        sim, bot, result = self.run_bot("Meme-Stock Gambler", client)
        for name, order, why in result.rejections:
            bot.rejected(order, why)
        sim.step(date(2026, 10, 20), {"Meme-Stock Gambler": bot})
        self.assertIn("Not enough cash", client.requests[1]["messages"][0]["content"])

    def test_failures_become_holds(self):
        for client in (FakeClient(error=ConnectionError("offline")), FakeClient(stop_reason="refusal"),
                       FakeClient("not json")):
            sim, bot, result = self.run_bot("Tech Growth Bull", client)
            self.assertEqual(result.fills, [])
            self.assertEqual(len(bot.feedback), 1)

    def test_missing_credentials_stop_the_run(self):
        from papertrade.brain import BrainUnavailable
        with self.assertRaises(BrainUnavailable):
            self.run_bot("Tech Growth Bull", FakeClient(error=TypeError("Could not resolve authentication method")))

    def test_unavailable_part_way_leaves_no_trades(self):
        from papertrade.brain import BrainUnavailable
        sim = make_sim()
        good = PersonaBot(BY_NAME["Value Investor"], ClaudeBrain(FakeClient(answer(("KO", "buy", 7000, "usd", "x")))))
        bad = PersonaBot(BY_NAME["Tech Growth Bull"], ClaudeBrain(FakeClient(error=TypeError("Could not resolve authentication method"))))
        with self.assertRaises(BrainUnavailable):
            sim.step(MON, {"Value Investor": good, "Tech Growth Bull": bad})
        self.assertEqual(sim.ledger.trades(), [])

    def test_journal_saved(self):
        ledger = Ledger()
        from papertrade import Account
        ledger.add_account(Account("Bob"))
        ledger.record_journal(MON, "Bob", "Bought the dip.", "Up.", ["one problem"])
        self.assertEqual(ledger.journal("Bob"), [(MON, "Up.", "Bought the dip.")])


if __name__ == "__main__":
    unittest.main()
