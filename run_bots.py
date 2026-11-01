"""Run the seven Claude-driven bots.

    python run_bots.py --synthetic --days 5    # offline random-walk prices, real Claude decisions
                                               # (run it again to carry on from where it stopped)
    python run_bots.py                         # today, using real data from fetch_data.py
    python run_bots.py --bots "Bitcoin Bob" "Contrarian Carl"   # just some of them
    python run_bots.py --synthetic --show-prompt "Bitcoin Bob"  # print what Claude would read, no API call

Uses your Claude subscription through Claude Code: install Claude Code, run `claude` once and sign
in with /login. No API key needed. (--backend api uses an Anthropic API key instead.)
Accounts, trades and journals are kept in the ledger file, so each run picks up where the last left off.
"""
import argparse
from datetime import date, timedelta

from papertrade import Account, Ledger, Simulation, SyntheticPriceFeed
import sys

from papertrade.brain import BrainUnavailable, make_brain, PersonaBot, system_prompt
from papertrade.briefing import build_briefing
from papertrade.desk import ResearchDesk
from papertrade.journal import JournalMemory
from papertrade.lab import system_notes
from papertrade.mining import JournalMiner
from papertrade.personas import BY_NAME
from papertrade.market_data import PriceCache
from papertrade.events import MarketCalendar
from papertrade.filings import FilingsClerk
from papertrade.news import NewsCache
from papertrade.personas import PERSONAS
from papertrade.universe import ASSETS, START_PRICES



def usage_note(backend: str, tokens_in: int, tokens_out: int) -> str:
    if backend == "api":  # Claude Opus 5.5 list prices, dollars per million tokens
        return f"About ${tokens_in / 1e6 * 4 + tokens_out / 1e6 * 20:.2f} in API charges."
    return "Covered by your Claude plan (counts toward its usage limits, no extra charge)."

SYNTHETIC_START = date(2026, 1, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="use random-walk prices instead of data.db")
    ap.add_argument("--days", type=int, default=1, help="days to simulate (synthetic mode)")
    ap.add_argument("--data", default="data.db", help="price and news cache from fetch_data.py")
    ap.add_argument("--ledger", help="where accounts and journals are kept")
    ap.add_argument("--bots", nargs="*", help="names of the bots to run (default: all seven)")
    ap.add_argument("--no-desk", action="store_true", help="traders read raw headlines; no research desk")
    ap.add_argument("--show-prompt", metavar="BOT", help="print one bot's prompt for the first day and stop")
    ap.add_argument("--force", action="store_true", help="run again even if today already ran")
    ap.add_argument("--backend", default="claude-code", choices=["claude-code", "api"],
                    help="claude-code (default) uses your Claude plan via Claude Code; api needs ANTHROPIC_API_KEY")
    ap.add_argument("--model", help="model alias or name (default: opus)")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = ap.parse_args()

    if args.synthetic:
        # Synthetic runs continue from the last simulated day in the ledger.
        ledger = Ledger(args.ledger or "synthetic.db")
        done = [d for n in ledger.account_names() for d, _, _ in ledger.snapshots(n)]
        start = max(done) + timedelta(days=1) if done else SYNTHETIC_START
        origin = SYNTHETIC_START - timedelta(days=400)  # enough history for 200-day lab indicators
        feed = SyntheticPriceFeed(ASSETS, START_PRICES, origin, (start - origin).days + args.days, seed=11)
        days, news, calendar, filings = [start + timedelta(days=i) for i in range(args.days)], None, None, None
    else:
        feed, news = PriceCache(args.data).feed(), NewsCache(args.data)
        calendar, filings = MarketCalendar(args.data), FilingsClerk(args.data)
        days, ledger = [date.today()], Ledger(args.ledger or "bots.db")

    if args.show_prompt:
        persona = BY_NAME[args.show_prompt]
        sim = Simulation(ASSETS, feed, ledger)  # read-only here: nothing is stepped or saved
        view = sim.view(days[0], sim.accounts.get(persona.name) or Account(persona.name))
        print("=== SYSTEM PROMPT ===\n" + system_prompt(persona))
        system = system_notes(ledger.db, persona, view)
        watch = list(persona.watchlist)
        print("\n=== USER MESSAGE ===\n" + build_briefing(
            view, watch, news, system=system,
            calendar=calendar.notes_for(days[0], watch) if calendar else "",
            filings=filings.notes_for(days[0], watch) if filings else "").text)
        return

    personas = [p for p in PERSONAS if not args.bots or p.name in args.bots]
    brain = make_brain(args.backend, args.effort, args.model)
    memory = JournalMemory(ledger)
    miner = JournalMiner(ledger)
    desk = None if args.no_desk else ResearchDesk(ledger, news, assets=ASSETS, calendar=calendar, filings=filings)
    bots = {p.name: PersonaBot(p, brain, news, memory, desk, calendar, filings) for p in personas}
    sim = Simulation(ASSETS, feed, ledger)
    tokens_in = tokens_out = 0

    for day in days:
        done = [n for n in bots if any(d == day for d, _, _ in ledger.snapshots(n))]
        if done and not args.force:
            print(f"{', '.join(done)} already traded on {day}. Use --force to run again, or --ledger for a new file.")
            return
        print(f"\n===== {day:%A %d %B %Y} =====")
        try:
            for bot in bots.values():
                for summary in memory.roll_up(bot.persona, brain, day):
                    print(f"\n*** {bot.persona.name} looks back on the {summary.label} ({summary.written_by}):")
                    print(f"  {summary.summary}")
                    for lesson in summary.lessons:
                        print(f"  Lesson: {lesson}")
            for report in miner.mine(brain, day):
                print(f"\n*** Journal mining for {report['month']}: {report['summary']}")
                for row in report["stats"]:
                    print(f"  {row['category']}: {row['wins']}/{row['trips']} won, ${row['total_pnl']:,.2f}")
            if desk:
                prices = feed.prices(day)
                open_symbols = [s for s, a in sim.assets.items() if a.is_open(day) and s in prices]
                for r in desk.publish(brain, day, lambda sym, n, d=day: feed.history(sym, d, n), open_symbols):
                    calls = ", ".join(f"{c['symbol']} {c['stance']}" for c in r.calls)
                    print(f"\n### {r.desk}: {r.headline}\n  View: {r.opinion}\n  Dissent: {r.dissent}\n  Calls: {calls}")
                for problem in desk.problems:
                    print(f"  DESK PROBLEM {problem}")
                desk.problems.clear()
            result = sim.step(day, bots)
        except BrainUnavailable as e:
            sys.exit(str(e))
        for name, order, why in result.rejections:
            bots[name].rejected(order, why)
        for name, ok, message in result.standing:
            if not ok:
                bots[name].standing_problem(message)
        for t in result.triggered:
            print(f"\n!!! {t.account}: standing order fired, {t.side} {t.quantity.normalize()} {t.symbol} @ {t.price}: {t.reason}")
        for name, bot in bots.items():
            d = bot.last_decision
            if d is None:
                print(f"\n--- {name}: markets closed, resting today")
                continue
            ledger.record_journal(day, name, d.journal_entry, d.market_view, bot.feedback)
            tokens_in += d.usage.get("input_tokens", 0)
            tokens_out += d.usage.get("output_tokens", 0)
            print(f"\n--- {name}: {d.market_view or '(no view)'}")
            for t in (t for t in result.fills if t.account == name):
                print(f"  FILLED {t.side} {t.quantity} {t.symbol} @ {t.price}: {t.reason}")
            for _, ok, message in (r for r in result.standing if r[0] == name and r[1]):
                print(f"  STANDING {message}")
            for problem in bot.feedback:
                print(f"  PROBLEM {problem}")
            if d.journal_entry:
                print(f"  Journal: {d.journal_entry}")

        print("\nLeaderboard:")
        for rank, (name, equity, pct) in enumerate(sim.leaderboard(day), 1):
            print(f"  {rank}. {name:<19} ${equity:>12,}  {pct:+}%")
        bench = next((b for b in (sim.benchmark_return(a, feed.prices(day)) for a in sim.accounts.values()) if b), None)
        if bench:
            print(f"     S&P 500 buy-and-hold since {bench[0]:%d %b}: {bench[1]:+.2f}%")

    for extra in (memory.usage, miner.usage, desk.usage if desk else {}):
        tokens_in += extra.get("input_tokens", 0)
        tokens_out += extra.get("output_tokens", 0)
    print(f"\nClaude usage: {tokens_in:,} input + {tokens_out:,} output tokens. " + usage_note(args.backend, tokens_in, tokens_out))


if __name__ == "__main__":
    main()
