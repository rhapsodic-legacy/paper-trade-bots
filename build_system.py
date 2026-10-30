"""Send bots to the strategy lab to design, test and adopt a trading system.

    python build_system.py "Momentum Trader"             # one bot, 3 design rounds
    python build_system.py --all --rounds 4               # every bot
    python build_system.py "Contrarian Carl" --test-start 2022-01-01
    python build_system.py --list                         # show each bot's current system

Reads price history from history.db (see import_history.py) and saves systems to the bots'
ledger, where their daily briefings pick them up. Each round is one Claude call, plus one for the
lab notebook at the end.
"""
import argparse
import json
import sys
from datetime import date

from papertrade import Ledger
from papertrade.brain import BrainUnavailable, make_brain
from papertrade.history import HistoryStore
from papertrade.lab import StrategyLab, active_system, describe
from papertrade.personas import BY_NAME, PERSONAS
from papertrade.strategy import Market, SpecError
from run_bots import usage_note



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bots", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--history", default="history.db")
    ap.add_argument("--ledger", default="bots.db")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--test-start", type=date.fromisoformat, help="first day of the unseen test years")
    ap.add_argument("--backend", default="claude-code", choices=["claude-code", "api"],
                    help="claude-code (default) uses your Claude plan via Claude Code; api needs ANTHROPIC_API_KEY")
    ap.add_argument("--model", help="model alias or name (default: opus)")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    ledger = Ledger(args.ledger)
    if args.list:
        for p in PERSONAS:
            s = active_system(ledger.db, p.name)
            if not s:
                print(f"{p.name}: no system yet")
                continue
            print(f"\n{p.name}: {s['spec']['name']} (built {s['created']}, trust {s['trust']}/5)\n  {s['spec']['idea']}")
            print(f"  Test years: {describe(s['test'])}\n  Notebook: {s['notebook']}")
        return

    store = HistoryStore(args.history)
    market = Market({s: store.series(s) for s, *_ in store.symbols()})
    if not market.data:
        sys.exit("history.db is empty. Run import_history.py first.")
    names = [p.name for p in PERSONAS] if args.all else args.bots
    if not names:
        sys.exit("Name a bot, or use --all.")
    brain = make_brain(args.backend, args.effort, args.model)
    lab = StrategyLab(ledger.db, market)
    for name in names:
        print(f"\n===== {name} enters the lab =====")
        try:
            result = lab.build(BY_NAME[name], brain, args.rounds, args.test_start)
        except BrainUnavailable as e:
            sys.exit(str(e))
        except SpecError as e:
            print(f"{name}: {e}")
            continue
        if result:
            print(f"\nSystem: {json.dumps(result.system, indent=1)}\n\nNotebook (trust {result.trust}/5): {result.notebook}")
    print(f"\nClaude usage: {lab.usage['input_tokens']:,} input + {lab.usage['output_tokens']:,} output tokens. "
          + usage_note(args.backend, lab.usage["input_tokens"], lab.usage["output_tokens"]))


if __name__ == "__main__":
    main()
