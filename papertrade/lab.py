"""The strategy lab: each bot designs its own trading system from history, and finds out honestly
whether it works.

1. The bot sees what history is available (training years only) and proposes a system in the
   rule language from strategy.py, in keeping with its personality.
2. The system is backtested on the training years. The bot sees the results, including
   buy-and-hold for comparison, and may refine it. This repeats for a few rounds.
3. The final version is tested once on later years the bot has never seen. That is the honest
   score: a system that only looked good on the years it was tuned on is overfitted, and the bot
   has to write that down in its lab notebook.
4. The system is saved, and from then on its signals appear in the bot's daily briefing as advice.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from . import strategy
from .personas import Persona
from .strategy import SYSTEM_SCHEMA, Market, SpecError, backtest, parse_system, signals

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}, "system": SYSTEM_SCHEMA},
    "required": ["reasoning", "system"],
    "additionalProperties": False,
}
NOTEBOOK_SCHEMA = {
    "type": "object",
    "properties": {"notebook": {"type": "string"}, "trust_level": {"type": "integer", "enum": [1, 2, 3, 4, 5]}},
    "required": ["notebook", "trust_level"],
    "additionalProperties": False,
}

LAB_TASK = """You are in the strategy lab. Design a mechanical trading system that fits your personality \
and that you think could genuinely beat simply buying and holding. You only see the training years; \
your final system will be tested once on later years you never see, so a system tuned to quirks of the \
training data will be exposed. Simple, robust ideas usually survive better than clever, finely tuned ones.

Write the system in this rule language:
{language}

Rules of the lab:
- symbols: choose from {allowed}. Other symbols with history you can reference with SYM@...: {reference}.
- direction: {direction}
- entry conditions must all be true to open a position; any exit condition closes it. Stops, take-profit \
and max_hold_days of 0 mean "not used".
- Position sizing: position_pct is the % of the account put into each new position; max_positions caps \
how many are open at once.
- This is round {round} of {rounds}.{final_note}

# Training data available
{data}
{history}"""

NOTEBOOK_TASK = """Your system has now been run once on the test years it was never tuned on. Write your \
lab notebook entry in your own voice, 120 to 250 words: compare training and test results honestly, \
say whether this looks like a real edge, luck or overfitting, and how much you'll trust its signals in \
live trading. Then rate that trust from 1 (ignore it) to 5 (lean on it heavily).

# Your system
{system}

# Training years {train}
{train_result}

# Test years {test} (never seen before)
{test_result}"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    created TEXT NOT NULL,
    spec TEXT NOT NULL,           -- JSON in the rule language
    train TEXT NOT NULL,          -- JSON: metrics and benchmark on the training years
    test TEXT NOT NULL,           -- JSON: metrics and benchmark on the unseen test years
    rounds TEXT NOT NULL,         -- JSON: every attempt and its training result
    notebook TEXT NOT NULL,
    trust INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""


@dataclass
class LabResult:
    account: str
    system: dict
    train: dict
    test: dict
    rounds: list[dict]
    notebook: str
    trust: int


def describe(result: dict) -> str:
    m, b = result["metrics"], result["benchmark"]
    return (f"{m['start']} to {m['end']}: system {m['total_return_pct']:+.1f}% total ({m['cagr_pct']:+.1f}%/yr), "
            f"max drawdown {m['max_drawdown_pct']:.1f}%, Sharpe {m['sharpe']}, {m['trades']} trades, "
            f"win rate {m['win_rate_pct']}%, average trade {m['avg_trade_pct']:+.2f}%. "
            f"Buy-and-hold of the same symbols: {b['total_return_pct']:+.1f}% ({b['cagr_pct']:+.1f}%/yr).")


def data_summary(market: Market, symbols, start: date, end: date) -> str:
    lines = []
    for s in symbols:
        rows = [r for r in market.data.get(s, []) if start <= r[0] <= end]
        if len(rows) < 2:
            continue
        vol_note = "" if all(r[2] is not None for r in rows) else ", no volume data"
        lines.append(f"- {s}: {rows[0][0]} to {rows[-1][0]}, {len(rows)} daily bars, "
                     f"{(rows[-1][1] / rows[0][1] - 1) * 100:+.0f}% over the period{vol_note}")
    return "\n".join(lines)


class StrategyLab:
    def __init__(self, ledger_db: sqlite3.Connection, market: Market):
        self.db, self.market = ledger_db, market
        self.db.executescript(SCHEMA)
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def split(self, symbols, test_start: date | None, test_fraction: float = 0.3):
        days = sorted({d for s in symbols for d in self.market.days.get(s, [])})
        if len(days) < 300:
            raise SpecError("need at least about a year and a half of daily history to split into train and test")
        first, last = days[0], days[-1]
        test_start = test_start or days[int(len(days) * (1 - test_fraction))]
        # Leave a year of warm-up so 200-day indicators are ready when training starts.
        train_start = min(first + timedelta(days=365), test_start - timedelta(days=180))
        return train_start, test_start - timedelta(days=1), test_start, last

    def _ask(self, brain, persona, message, schema):
        from .brain import system_prompt
        raw, problem, usage = brain.ask(system_prompt(persona), message, schema)
        for k in self.usage:
            self.usage[k] += usage.get(k, 0)
        return raw, problem

    def build(self, persona: Persona, brain, rounds: int = 3, test_start: date | None = None,
              log=print) -> LabResult | None:
        allowed = [s for s in persona.watchlist if s in self.market.data]
        if not allowed:
            log(f"{persona.name}: no history for any of {persona.watchlist}; import some first")
            return None
        reference = sorted(set(self.market.data) - set(allowed))
        train_start, train_end, test_from, test_end = self.split(allowed, test_start)
        attempts = []
        for n in range(1, rounds + 1):
            history = ""
            if attempts:
                history = "\n# Your earlier attempts (training years only)\n" + "\n\n".join(
                    f"Attempt {a['round']}: {json.dumps(a['system'])}\nResult: {a['result']}" for a in attempts)
            final_note = (" This is the final round: submit the version you trust most to hold up on unseen "
                          "years, which need not be the one with the best training numbers." if n == rounds else "")
            message = LAB_TASK.format(
                language=strategy.__doc__.split("Trades are decided")[0].strip(),
                allowed=", ".join(allowed), reference=", ".join(reference) or "none",
                direction="long, or short (you may short)" if persona.can_short else "long only",
                round=n, rounds=rounds, final_note=final_note,
                data=data_summary(self.market, allowed + reference, train_start, train_end), history=history)
            raw, problem = self._ask(brain, persona, message, PROPOSAL_SCHEMA)
            if raw is None:
                log(f"{persona.name} round {n}: {problem}")
                continue
            try:
                system = parse_system(raw["system"], set(self.market.data), set(allowed), persona.can_short)
                result = describe(backtest(system, self.market, train_start, train_end))
            except SpecError as e:
                result = f"REJECTED: {e}"
            attempts.append({"round": n, "system": raw["system"], "reasoning": raw.get("reasoning", ""),
                             "result": result})
            log(f"{persona.name} round {n}: {raw['system'].get('name')}: {result}")

        usable = [a for a in attempts if not a["result"].startswith("REJECTED")]
        if not usable:
            log(f"{persona.name}: no usable system came out of the lab")
            return None
        final = usable[-1]
        system = parse_system(final["system"], set(self.market.data), set(allowed), persona.can_short)
        train = backtest(system, self.market, train_start, train_end)
        test = backtest(system, self.market, test_from, test_end)
        raw, _ = self._ask(brain, persona, NOTEBOOK_TASK.format(
            system=json.dumps(final["system"], indent=1), train=f"{train_start} to {train_end}",
            train_result=describe(train), test=f"{test_from} to {test_end}", test_result=describe(test)),
            NOTEBOOK_SCHEMA)
        notebook = raw["notebook"] if raw else "(Claude was unavailable to write the notebook.)"
        trust = int(raw["trust_level"]) if raw else 1
        res = LabResult(persona.name, final["system"],
                        {"metrics": train["metrics"], "benchmark": train["benchmark"]},
                        {"metrics": test["metrics"], "benchmark": test["benchmark"]}, attempts, notebook, trust)
        self.save(res)
        log(f"{persona.name} test years: {describe(test)}")
        return res

    def save(self, r: LabResult) -> None:
        with self.db:
            self.db.execute("UPDATE systems SET active = 0 WHERE account = ?", (r.account,))
            self.db.execute("INSERT INTO systems (account, created, spec, train, test, rounds, notebook, trust)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (r.account, date.today().isoformat(), json.dumps(r.system), json.dumps(r.train),
                             json.dumps(r.test), json.dumps(r.rounds), r.notebook, r.trust))


def active_system(db: sqlite3.Connection, account: str) -> dict | None:
    db.executescript(SCHEMA)
    row = db.execute("SELECT spec, train, test, notebook, trust, created FROM systems WHERE account = ? AND active = 1"
                     " ORDER BY id DESC LIMIT 1", (account,)).fetchone()
    if not row:
        return None
    spec, train, test, notebook, trust, created = row
    return {"spec": json.loads(spec), "train": json.loads(train), "test": json.loads(test),
            "notebook": notebook, "trust": trust, "created": created}


def system_notes(db: sqlite3.Connection, persona: Persona, view) -> str:
    """The 'your trading system' section of a bot's daily briefing."""
    found = active_system(db, persona.name)
    if not found:
        return ""
    spec = found["spec"]
    refs = set(spec["symbols"])
    for c in spec["entry"] + spec["exit"]:
        for side in (c["left"], c["right"]):
            if "@" in side:
                refs.add(side.split("@")[0])
    market = Market({s: [(d, float(c), None) for d, c in view.feed.bars(s, view.day, 300)] for s in refs})
    try:
        system = parse_system(spec, set(market.data), set(spec["symbols"]), True)
        lines = signals(system, market, view.day, {s for s in view.account.positions})
    except SpecError as e:
        lines = [f"can't evaluate today: {e}"]
    t = found["test"]["metrics"]
    b = found["test"]["benchmark"]
    return (f"{spec['name']}: {spec['idea']}\n"
            f"On unseen test years it made {t['total_return_pct']:+.1f}% vs {b['total_return_pct']:+.1f}% for "
            f"buy-and-hold (max drawdown {t['max_drawdown_pct']:.1f}%). Your trust in it: {found['trust']}/5.\n"
            "Today's signals (advice, not orders; you decide):\n" + "\n".join(f"- {l}" for l in lines))
