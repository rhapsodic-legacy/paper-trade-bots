"""A bot's brain: Claude reads the daily briefing in character and answers with trades and a journal entry.

The answer is constrained to a JSON schema (structured outputs), then checked again here before any
order reaches the broker. Anything that goes wrong (no API key, a network error, a refusal, an order
for a symbol the persona doesn't trade) turns into a "hold" with the reason written down, so a bad
day for the API never crashes the simulation.
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .broker import Order, Side
from .briefing import build_briefing
from .lab import system_notes
from .personas import Persona
from .sim import MarketView

MODEL = "claude-opus-5-5"
EFFORT = "medium"  # low | medium | high | xhigh | max: higher thinks harder and costs more

RULES = """You are one of seven AI trading bots in Paper_Trade, an educational stock and crypto \
simulator. Every bot started with $100,000 of pretend money. People watch the bots to learn how \
trading works, how AI makes decisions, and where AI falls short, so be honest about uncertainty and \
mistakes. Nothing you do involves real money.

Each day you get a briefing with your account, prices and news. Decide what to trade, if anything, \
staying true to your personality while genuinely trying to make money over time.

How orders work:
- Orders fill at today's closing price shown in the briefing. You can only trade symbols that are \
open today and on your watchlist.
- side is one of buy, sell, short, cover. sell reduces a long position; cover reduces a short one.
- unit "usd" means amount is dollars to spend or sell; "units" means shares or coins; "all" closes \
the whole position (amount is ignored).
- You can't spend more cash than you have. Holding (no orders) is a perfectly good decision.
- Close a long before shorting the same symbol, and cover a short before buying it.

Some days your briefing carries research desk notes instead of raw headlines: each desk's summary, \
house view, the strongest dissent, its calls and its track record. Weigh them with your own judgement; \
desks can be wrong, and a dissent is sometimes the better argument.

Write journal_entry in your own voice, in first person, 80 to 200 words: what you noticed, what you \
did and why, how you feel about it, and anything you want to remember for next time. market_view is \
one sentence on where you think things are heading."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string"},
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": [s.value for s in Side]},
                    "amount": {"type": "number"},
                    "unit": {"type": "string", "enum": ["usd", "units", "all"]},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "side", "amount", "unit", "reason"],
                "additionalProperties": False,
            },
        },
        "journal_entry": {"type": "string"},
    },
    "required": ["market_view", "orders", "journal_entry"],
    "additionalProperties": False,
}


class BrainUnavailable(Exception):
    """Claude can't be reached at all (no API key, bad key). The run should stop, not record a day of holds."""


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "lessons"],
    "additionalProperties": False,
}

SUMMARY_TASK = """Look back over your {period} as a trader and write your {kind} journal summary, \
in your own voice and first person, 100 to 250 words. The numbers in the facts section are exact; \
use them and don't invent others. Say what happened, which calls worked and which didn't, and how \
honest you were with yourself. Then give 1 to 4 short lessons you want to carry forward: concrete \
habits or rules, not slogans. It's fine to conclude you were lucky, or that you were wrong.

# Facts for {period}
{facts}

# Your entries for {period}
{entries}"""


@dataclass
class Decision:
    orders: list[Order]
    journal_entry: str
    market_view: str = ""
    problems: list[str] = field(default_factory=list)  # orders dropped, API errors, refusals
    usage: dict = field(default_factory=dict)


def system_prompt(persona: Persona) -> str:
    extras = []
    if persona.can_short:
        extras.append("You are allowed to short (side short, then cover later).")
    else:
        extras.append("You do not short. Only buy and sell.")
    return (f"{RULES}\n\n# Who you are: {persona.name} ({persona.tagline})\n{persona.character}\n"
            f"Your watchlist: {', '.join(persona.watchlist)}. {' '.join(extras)}")


def to_orders(raw: dict, persona: Persona, view: MarketView) -> tuple[list[Order], list[str]]:
    """Turn Claude's JSON orders into broker Orders, dropping any that break the persona's rules."""
    orders, problems = [], []
    for o in raw.get("orders", []):
        symbol, side = str(o.get("symbol", "")).upper(), o.get("side")
        label = f"{side} {symbol}"
        asset = view.assets.get(symbol)
        if symbol not in persona.watchlist or asset is None:
            problems.append(f"Dropped {label}: not on your watchlist")
            continue
        if asset.asset_class not in persona.asset_classes:
            problems.append(f"Dropped {label}: {persona.name} doesn't trade {asset.asset_class.value}")
            continue
        if side == "short" and not persona.can_short:
            problems.append(f"Dropped {label}: {persona.name} doesn't short")
            continue
        if symbol not in view.open_symbols:
            problems.append(f"Dropped {label}: market closed today")
            continue
        held = view.account.position(symbol).quantity
        try:
            if o.get("unit") == "all":
                qty = abs(held)
            elif o.get("unit") == "usd":
                qty = Decimal(str(o["amount"])) / view.prices[symbol]
            else:
                qty = Decimal(str(o["amount"]))
        except (InvalidOperation, KeyError, ZeroDivisionError):
            problems.append(f"Dropped {label}: unreadable amount {o.get('amount')!r}")
            continue
        if qty <= 0:
            problems.append(f"Dropped {label}: nothing to trade")
            continue
        orders.append(Order(symbol, Side(side), qty, str(o.get("reason", ""))[:500]))
    return orders, problems


def _usage(u) -> dict:
    """Token counts from an API response or Claude Code's JSON. Cached prompt tokens are input too."""
    get = (lambda k: (u or {}).get(k) or 0) if isinstance(u, dict) or u is None else (lambda k: getattr(u, k, 0) or 0)
    return {"input_tokens": get("input_tokens") + get("cache_read_input_tokens") + get("cache_creation_input_tokens"),
            "output_tokens": get("output_tokens")}


def _is_auth_problem(e: Exception) -> bool:
    if isinstance(e, TypeError) and "authentication" in str(e):  # SDK found no credentials at all
        return True
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError))


class ClaudeBrain:
    def __init__(self, client=None, model: str = MODEL, effort: str = EFFORT):
        if client is None:
            import anthropic  # imported here so steps 1 and 2 keep working without the SDK
            client = anthropic.Anthropic()
        self.client, self.model, self.effort = client, model, effort

    def ask(self, system: str, message: str, schema: dict) -> tuple[dict | None, str, dict]:
        """One structured Claude call. Returns (answer, problem, usage); answer is None on failure."""
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": message}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines, the API retries on its recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as e:
            if _is_auth_problem(e):
                raise BrainUnavailable(f"Can't use the Claude API: {e}") from e
            # Network, rate limit, server error: skip this call, don't stop the run.
            return None, f"Claude API call failed: {type(e).__name__}: {e}", {}

        usage = _usage(response.usage)
        if response.stop_reason == "refusal":
            return None, "Claude declined to answer.", usage
        if response.stop_reason == "max_tokens":
            return None, "Claude's answer was cut off.", usage
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text), "", usage
        except json.JSONDecodeError:
            return None, "Claude's answer wasn't valid JSON.", usage

    def decide(self, persona: Persona, briefing_text: str, view: MarketView) -> Decision:
        raw, problem, usage = self.ask(system_prompt(persona), briefing_text, DECISION_SCHEMA)
        if raw is None:
            return Decision([], "", problems=[f"{problem} Held for the day."], usage=usage)
        orders, problems = to_orders(raw, persona, view)
        return Decision(orders, raw.get("journal_entry", ""), raw.get("market_view", ""), problems, usage)

    def summarize(self, persona: Persona, kind: str, period: str, facts: str,
                  entries: str) -> tuple[str, list[str], dict] | None:
        """Write a weekly or monthly summary in character. None if Claude couldn't."""
        message = SUMMARY_TASK.format(kind=kind, period=period, facts=facts, entries=entries)
        raw, _, usage = self.ask(system_prompt(persona), message, SUMMARY_SCHEMA)
        if raw is None:
            return None
        return raw.get("summary", ""), [str(x) for x in raw.get("lessons", [])][:4], usage


class ClaudeCodeBrain(ClaudeBrain):
    """Asks Claude through the Claude Code command line instead of the API.

    Claude Code signs in with your Claude subscription (Pro or Max), so no API key or API billing
    is involved: calls count against the plan's usage limits instead. Install Claude Code, run
    `claude` once and sign in with /login, then the bots can use it.
    """

    def __init__(self, model: str = "opus", effort: str = EFFORT, command: str = "claude",
                 timeout: int = 600, runner=None):
        import shutil
        import subprocess
        # shutil.which finds claude.cmd on Windows too.
        self.model, self.effort, self.command, self.timeout = model, effort, shutil.which(command) or command, timeout
        self.run = runner or subprocess.run

    def ask(self, system: str, message: str, schema: dict) -> tuple[dict | None, str, dict]:
        import os
        import tempfile
        cmd = [self.command, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt", system, "--tools", "", "--model", self.model, "--effort", self.effort,
               "--no-session-persistence", "--strict-mcp-config"]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # make sure it uses the subscription login, never API billing
        try:
            done = self.run(cmd, input=message, capture_output=True, text=True, timeout=self.timeout,
                            env=env, cwd=tempfile.gettempdir())  # outside the project, so no CLAUDE.md is loaded
        except FileNotFoundError:
            raise BrainUnavailable("Claude Code isn't installed (no 'claude' command). Install it from "
                                   "claude.com/claude-code, run `claude`, and sign in with your plan.")
        except Exception as e:  # timeout and the like: skip this call, keep the run going
            return None, f"Claude Code call failed: {type(e).__name__}: {e}", {}

        out = {}
        try:
            out = json.loads(done.stdout or "{}")
        except json.JSONDecodeError:
            pass
        text = (out.get("result") or done.stderr or done.stdout or "").strip()
        usage = _usage(out.get("usage"))
        if done.returncode != 0 or out.get("is_error"):
            low = text.lower()
            if any(k in low for k in ("/login", "not logged in", "invalid api key", "authentication")):
                raise BrainUnavailable(f"Claude Code isn't signed in: {text[:200]}. Run `claude` and use /login.")
            if any(k in low for k in ("usage limit", "rate limit", "limit reached")):
                raise BrainUnavailable(f"Your Claude plan's usage limit was reached: {text[:200]}. Try again later.")
            return None, f"Claude Code returned an error: {text[:300]}", usage
        answer = out.get("structured_output")
        if answer is None:  # older Claude Code: the JSON comes back as the text result
            body = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            try:
                answer = json.loads(body)
            except json.JSONDecodeError:
                return None, "Claude's answer wasn't valid JSON.", usage
        if not isinstance(answer, dict):
            return None, "Claude's answer wasn't a JSON object.", usage
        return answer, "", usage


def make_brain(backend: str = "claude-code", effort: str = EFFORT, model: str | None = None):
    """'claude-code' uses your Claude subscription through the Claude Code CLI; 'api' uses an API key."""
    if backend == "api":
        return ClaudeBrain(model=model or MODEL, effort=effort)
    return ClaudeCodeBrain(model=model or "opus", effort=effort)


class PersonaBot:
    """Glues a persona, its brain, its memory and the briefing together into a Simulation strategy."""

    def __init__(self, persona: Persona, brain, news=None, memory=None, desk=None):
        self.persona, self.brain, self.news, self.memory, self.desk = persona, brain, news, memory, desk
        self.feedback: list[str] = memory.last_problems(persona.name) if memory else []
        self.last_decision: Decision | None = None  # None when its markets were all closed

    def __call__(self, view: MarketView) -> list[Order]:
        if not any(s in view.open_symbols for s in self.persona.watchlist):
            self.last_decision = None  # nothing it could trade today, so don't spend an API call
            return []
        recall = self.memory.recall(self.persona.name, view.day) if self.memory else ""
        research = self.desk.notes_for(self.persona.watchlist, view.day) if self.desk else ""
        system = system_notes(self.memory.db, self.persona, view) if self.memory else ""
        briefing = build_briefing(view, list(self.persona.watchlist), self.news, self.feedback, recall, research,
                                  system)
        self.feedback = []
        self.last_decision = self.brain.decide(self.persona, briefing.text, view)
        self.feedback.extend(self.last_decision.problems)
        return self.last_decision.orders

    def rejected(self, order: Order, why: str) -> None:
        self.feedback.append(f"{order.side.value} {order.quantity} {order.symbol}: {why}")
