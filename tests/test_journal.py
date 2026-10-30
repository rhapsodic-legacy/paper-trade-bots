"""Journal memory tests. A scripted fake stands in for Claude, so no API calls are made."""
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

from papertrade import Ledger, Simulation, SyntheticPriceFeed
from papertrade.brain import SUMMARY_SCHEMA, ClaudeBrain, PersonaBot
from papertrade.journal import JournalMemory, month_bounds, week_bounds
from papertrade.personas import BY_NAME
from papertrade.universe import ASSETS, START_PRICES

START = date(2026, 9, 28)  # a Monday


class ScriptedClaude:
    """Answers trade requests with a small BTC buy, and summary requests with a numbered summary."""

    def __init__(self, fail_summaries=False):
        self.requests, self.fail_summaries = [], fail_summaries
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        if kw["output_config"]["format"]["schema"] is SUMMARY_SCHEMA:
            if self.fail_summaries:
                raise ConnectionError("offline")
            n = sum(r["output_config"]["format"]["schema"] is SUMMARY_SCHEMA for r in self.requests)
            body = {"summary": f"Summary number {n}.", "lessons": [f"Lesson {n}", "Buy dips slowly"]}
        else:
            day = kw["messages"][0]["content"].splitlines()[0]
            body = {"market_view": "Sideways.", "journal_entry": f"Entry for {day[-20:]}.",
                    "orders": [{"symbol": "BTC", "side": "buy", "amount": 100, "unit": "usd", "reason": "stack"}]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def run(days, client, name="Bitcoin Bob", ledger=None):
    ledger = ledger or Ledger()
    feed = SyntheticPriceFeed(ASSETS, START_PRICES, START - timedelta(days=30), 30 + days + 1, seed=3)
    sim, memory, brain = Simulation(ASSETS, feed, ledger), JournalMemory(ledger), ClaudeBrain(client)
    bot = PersonaBot(BY_NAME[name], brain, memory=memory)
    for i in range(days):
        day = START + timedelta(days=i)
        memory.roll_up(bot.persona, brain, day)
        sim.step(day, {name: bot})
        if bot.last_decision:
            ledger.record_journal(day, name, bot.last_decision.journal_entry, bot.last_decision.market_view)
    return sim, memory, ledger


class PeriodTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(week_bounds(date(2026, 10, 1)), (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(month_bounds(date(2026, 2, 10)), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(month_bounds(date(2026, 12, 31)), (date(2026, 12, 1), date(2026, 12, 31)))


class RollUpTest(unittest.TestCase):
    def test_weekly_and_monthly_summaries_are_written_once(self):
        client = ScriptedClaude()
        sim, memory, ledger = run(36, client)  # 28 Sep .. 2 Nov
        weeks = memory.summaries("Bitcoin Bob", "week")
        months = memory.summaries("Bitcoin Bob", "month")
        self.assertEqual([w.start for w in weeks], [START + timedelta(weeks=i) for i in range(5)])
        self.assertEqual([(m.start, m.end) for m in months],
                         [(date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))])
        # Running roll-up again changes nothing.
        before = len(client.requests)
        memory.roll_up(BY_NAME["Bitcoin Bob"], ClaudeBrain(client), date(2026, 11, 3))
        self.assertEqual(len(client.requests), before)

    def test_facts_come_from_the_ledger(self):
        sim, memory, ledger = run(8, ScriptedClaude())
        week = memory.summaries("Bitcoin Bob", "week")[0]
        self.assertIn("Trades: 7 (7 opening, 0 closing)", week.facts)
        prompt = memory.facts("Bitcoin Bob", week.start, week.end)
        self.assertEqual(prompt, week.facts)

    def test_month_summary_reads_the_weeks(self):
        client = ScriptedClaude()
        run(36, client)
        october = [r for r in client.requests
                   if r["messages"][0]["content"].startswith("Look back over your October 2026")][0]
        self.assertIn("[week of 05 Oct 2026] Summary number", october["messages"][0]["content"])

    def test_claude_down_keeps_facts_and_retries_later(self):
        ledger = Ledger()
        sim, memory, _ = run(8, ScriptedClaude(fail_summaries=True), ledger=ledger)
        week = memory.summaries("Bitcoin Bob", "week")[0]
        self.assertEqual(week.written_by, "facts only")
        memory.roll_up(BY_NAME["Bitcoin Bob"], ClaudeBrain(ScriptedClaude()), date(2026, 10, 6))
        self.assertEqual(memory.summaries("Bitcoin Bob", "week")[0].written_by, "claude")


class RecallTest(unittest.TestCase):
    def test_briefing_carries_memory(self):
        client = ScriptedClaude()
        run(15, client)
        briefing = client.requests[-1]["messages"][0]["content"]
        self.assertIn("## Your memory", briefing)
        self.assertIn("Lessons you chose to keep:", briefing)
        self.assertEqual(briefing.count("- Buy dips slowly"), 1)  # repeated lessons appear once
        self.assertIn("Your summary of the week of 05 Oct 2026", briefing)
        self.assertIn("Your last few daily entries:", briefing)

    def test_memory_never_includes_the_future(self):
        sim, memory, ledger = run(15, ScriptedClaude())
        recall = memory.recall("Bitcoin Bob", date(2026, 10, 3))
        self.assertIn("Your summary of September 2026", recall)  # September was over by then
        self.assertNotIn("week of 28 Sep", recall)               # that week ends on 4 Oct
        self.assertIn("Fri 02 Oct", recall)
        self.assertNotIn("Sat 03 Oct", recall)

    def test_stock_bot_does_not_journal_weekends(self):
        sim, memory, ledger = run(7, ScriptedClaude(), name="Value Investor")
        self.assertEqual(len(ledger.journal("Value Investor")), 5)


class FeedTest(unittest.TestCase):
    def test_feed_is_newest_first_with_summaries(self):
        sim, memory, ledger = run(9, ScriptedClaude())
        feed = memory.feed("Bitcoin Bob")
        self.assertEqual(feed[0][0], START + timedelta(days=8))
        kinds = [k for _, k, _, _ in feed]
        self.assertEqual(kinds.count("week"), 1)
        self.assertEqual(kinds.count("day"), 9)
        week_item = next(i for i in feed if i[1] == "week")
        self.assertIn("The numbers:", week_item[3])

    def test_problems_survive_between_runs(self):
        ledger = Ledger()
        from papertrade import Account
        ledger.add_account(Account("Bitcoin Bob"))
        ledger.record_journal(START, "Bitcoin Bob", "x", "", ["buy 3 BTC: Not enough cash"])
        bot = PersonaBot(BY_NAME["Bitcoin Bob"], ClaudeBrain(ScriptedClaude()), memory=JournalMemory(ledger))
        self.assertEqual(bot.feedback, ["buy 3 BTC: Not enough cash"])


if __name__ == "__main__":
    unittest.main()
