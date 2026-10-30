"""Trading systems the bots design, and the backtester that tests them.

A bot never writes code that we run. It writes a system in a small rule language (JSON), which
this module checks and then simulates. That keeps the lab safe and makes every system readable
by the people watching.

Indicators (N is a look-back in trading days, 2 to 250):
  price          today's close
  sma:N, ema:N   simple / exponential moving average of the close
  rsi:N          relative strength index, 0 to 100
  return:N       % change in price over the last N days
  vol:N          volatility: standard deviation of daily % changes over N days
  zscore:N       how many standard deviations the price is from its N-day average
  high:N, low:N  highest / lowest close of the previous N days (not counting today)
  drawdown:N     % the price is below its N-day high (0 or negative)
  volume_ratio:N today's volume divided by the average of the previous N days
Prefix with another symbol to look across markets, e.g. "SPY@price" or "SPY@sma:200".
A condition compares two of these, or one with a number: {"left": "rsi:14", "op": "<", "right": "30"}.
Ops: > < >= <= crosses_above crosses_below.

Trades are decided on a day's close and filled at the next trading day's close, so a system can
never act on a price it couldn't have known. Every fill pays a fee (default 0.1%) to stand in for
commission and slippage.
"""
import math
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

OPS = [">", "<", ">=", "<=", "crosses_above", "crosses_below"]
KINDS = {"price", "sma", "ema", "rsi", "return", "vol", "zscore", "high", "low", "drawdown", "volume_ratio"}
TOKEN = re.compile(r"^(?:(?P<sym>[A-Z]{1,6})@)?(?P<kind>[a-z_]+)(?::(?P<n>\d+))?$")

CONDITION_SCHEMA = {
    "type": "object",
    "properties": {"left": {"type": "string"}, "op": {"type": "string", "enum": OPS}, "right": {"type": "string"}},
    "required": ["left", "op", "right"],
    "additionalProperties": False,
}

SYSTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "idea": {"type": "string"},
        "symbols": {"type": "array", "items": {"type": "string"}},
        "direction": {"type": "string", "enum": ["long", "short"]},
        "entry": {"type": "array", "items": CONDITION_SCHEMA},
        "exit": {"type": "array", "items": CONDITION_SCHEMA},
        "position_pct": {"type": "number"},
        "max_positions": {"type": "integer"},
        "stop_loss_pct": {"type": "number"},
        "take_profit_pct": {"type": "number"},
        "max_hold_days": {"type": "integer"},
    },
    "required": ["name", "idea", "symbols", "direction", "entry", "exit", "position_pct", "max_positions",
                 "stop_loss_pct", "take_profit_pct", "max_hold_days"],
    "additionalProperties": False,
}


class SpecError(ValueError):
    pass


# ---------- indicators ----------

def _sma(c, n):
    out, s = [None] * len(c), 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(c, n):
    out, k, e = [None] * len(c), 2 / (n + 1), None
    for i, v in enumerate(c):
        e = v if e is None else v * k + e * (1 - k)
        if i >= n - 1:
            out[i] = e
    return out


def _rsi(c, n):
    out, gain, loss = [None] * len(c), 0.0, 0.0
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gain += g / n
            loss += l / n
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
        if i >= n:
            out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def _window(c, n, fn, include_today=True):
    out = [None] * len(c)
    for i in range(len(c)):
        lo, hi = (i - n + 1, i + 1) if include_today else (i - n, i)
        if lo >= 0:
            out[i] = fn(c[lo:hi])
    return out


def _std(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def indicator(kind: str, n: int, closes: list[float], volumes: list) -> list:
    if kind == "price":
        return list(closes)
    if kind == "sma":
        return _sma(closes, n)
    if kind == "ema":
        return _ema(closes, n)
    if kind == "rsi":
        return _rsi(closes, n)
    if kind == "return":
        return [None if i < n else (closes[i] / closes[i - n] - 1) * 100 for i in range(len(closes))]
    if kind == "vol":
        rets = [None] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        return [None if i < n else _std(rets[i - n + 1:i + 1]) for i in range(len(closes))]
    if kind == "zscore":
        out = []
        for i in range(len(closes)):
            if i < n - 1:
                out.append(None)
                continue
            w = closes[i - n + 1:i + 1]
            sd = _std(w)
            out.append(0.0 if sd == 0 else (closes[i] - sum(w) / n) / sd)
        return out
    if kind == "high":
        return _window(closes, n, max, include_today=False)
    if kind == "low":
        return _window(closes, n, min, include_today=False)
    if kind == "drawdown":
        hi = _window(closes, n, max)
        return [None if h is None else (closes[i] / h - 1) * 100 for i, h in enumerate(hi)]
    if kind == "volume_ratio":
        if any(v is None for v in volumes):
            return [None] * len(closes)
        avg = _window(volumes, n, lambda w: sum(w) / n, include_today=False)
        return [None if a in (None, 0) else volumes[i] / a for i, a in enumerate(avg)]
    raise SpecError(f"unknown indicator {kind}")


# ---------- the rule language ----------

@dataclass(frozen=True)
class Ref:
    symbol: str | None  # None means "the symbol being traded"
    kind: str
    n: int


def parse_operand(text: str, symbols: set[str]) -> Ref | float:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = TOKEN.match(text)
    if not m or m["kind"] not in KINDS:
        raise SpecError(f"'{text}' isn't a number or a known indicator ({', '.join(sorted(KINDS))})")
    kind, n = m["kind"], int(m["n"]) if m["n"] else 0
    if kind != "price" and not 2 <= n <= 250:
        raise SpecError(f"'{text}' needs a look-back between 2 and 250, like {kind}:20")
    if m["sym"] and m["sym"] not in symbols:
        raise SpecError(f"'{text}' refers to {m['sym']}, which has no price history here")
    return Ref(m["sym"], kind, n)


@dataclass
class System:
    name: str
    idea: str
    symbols: list[str]
    direction: str
    entry: list[tuple]
    exit: list[tuple]
    position_pct: float
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_days: int
    raw: dict = field(default_factory=dict)


def parse_system(raw: dict, available: set[str], allowed_symbols: set[str], can_short: bool) -> System:
    """Check a system the bot proposed. Raises SpecError with a message the bot can act on."""
    symbols = [s.upper() for s in raw.get("symbols", [])]
    bad = [s for s in symbols if s not in allowed_symbols]
    if bad:
        raise SpecError(f"symbols {bad} aren't on your list ({sorted(allowed_symbols)})")
    missing = [s for s in symbols if s not in available]
    if missing:
        raise SpecError(f"no price history for {missing}; available: {sorted(available & allowed_symbols)}")
    if not symbols:
        raise SpecError("pick at least one symbol")
    if raw.get("direction") == "short" and not can_short:
        raise SpecError("your personality doesn't short; use direction long")
    if not raw.get("entry"):
        raise SpecError("entry needs at least one condition")
    conds = lambda key: [(parse_operand(c["left"], available), c["op"], parse_operand(c["right"], available))
                         for c in raw.get(key, [])]
    entry, exit_ = conds("entry"), conds("exit")
    for left, op, right in entry + exit_:
        if op not in OPS:
            raise SpecError(f"unknown op {op}")
        if isinstance(left, float) and isinstance(right, float):
            raise SpecError("a condition must use at least one indicator")
    pct = float(raw.get("position_pct", 0))
    if not 1 <= pct <= 100:
        raise SpecError("position_pct must be between 1 and 100")
    max_pos = int(raw.get("max_positions", 1))
    if not 1 <= max_pos <= 10:
        raise SpecError("max_positions must be between 1 and 10")
    return System(raw.get("name", "Unnamed"), raw.get("idea", ""), symbols, raw["direction"], entry, exit_,
                  pct, max_pos, max(0.0, float(raw.get("stop_loss_pct", 0))),
                  max(0.0, float(raw.get("take_profit_pct", 0))), max(0, int(raw.get("max_hold_days", 0))), raw)


# ---------- evaluating rules on data ----------

class Market:
    """Price history for a set of symbols, with indicators computed once and looked up by date."""

    def __init__(self, data: dict[str, list[tuple[date, float, float | None]]]):
        self.data = {s: rows for s, rows in data.items() if rows}
        self.days = {s: [r[0] for r in rows] for s, rows in self.data.items()}
        self._cache = {}

    def series(self, symbol: str, kind: str, n: int) -> list:
        key = (symbol, kind, n)
        if key not in self._cache:
            rows = self.data[symbol]
            self._cache[key] = indicator(kind, n, [r[1] for r in rows], [r[2] for r in rows])
        return self._cache[key]

    def index(self, symbol: str, day: date) -> int | None:
        """Position of the latest bar on or before `day`."""
        i = bisect_right(self.days[symbol], day) - 1
        return i if i >= 0 else None

    def value(self, operand, symbol: str, day: date, back: int = 0):
        if isinstance(operand, float):
            return operand
        sym = operand.symbol or symbol
        if sym not in self.data:
            return None
        i = self.index(sym, day)
        if i is None or i - back < 0:
            return None
        return self.series(sym, operand.kind, operand.n)[i - back]

    def check(self, cond, symbol: str, day: date) -> bool:
        left, op, right = cond
        a, b = self.value(left, symbol, day), self.value(right, symbol, day)
        if a is None or b is None:
            return False
        if op in ("crosses_above", "crosses_below"):
            pa, pb = self.value(left, symbol, day, 1), self.value(right, symbol, day, 1)
            if pa is None or pb is None:
                return False
            return (pa <= pb and a > b) if op == "crosses_above" else (pa >= pb and a < b)
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]

    def explain(self, cond, symbol: str, day: date) -> str:
        left, op, right = cond
        fmt = lambda o: f"{o:g}" if isinstance(o, float) else (f"{o.symbol}@" if o.symbol else "") + o.kind + (f":{o.n}" if o.n else "")
        val = lambda o: self.value(o, symbol, day)
        show = lambda v: "n/a" if v is None else f"{v:,.2f}"
        return f"{fmt(left)} ({show(val(left))}) {op} {fmt(right)} ({show(val(right))})"


# ---------- backtesting ----------

@dataclass
class Position:
    symbol: str
    qty: float          # negative for shorts
    entry_price: float
    entry_day: date


def backtest(system: System, market: Market, start: date, end: date, cash: float = 100_000.0,
             fee: float = 0.001) -> dict:
    initial = cash
    calendar = sorted({d for s in system.symbols for d in market.days[s] if start <= d <= end})
    if len(calendar) < 2:
        raise SpecError("not enough price history in that period")
    sign = -1 if system.direction == "short" else 1
    positions: dict[str, Position] = {}
    pending_exit, pending_entry = set(), []
    trades, curve = [], []
    last_price = {}

    def equity():
        return cash + sum(p.qty * last_price[p.symbol] for p in positions.values())

    for day in calendar:
        trading_today = [s for s in system.symbols if market.index(s, day) is not None
                         and market.days[s][market.index(s, day)] == day]
        for s in trading_today:
            last_price[s] = market.data[s][market.index(s, day)][1]

        # 1. Fill yesterday's decisions at today's close.
        for s in [s for s in pending_exit if s in trading_today]:
            p = positions.pop(s)
            price = last_price[s]
            cash += p.qty * price - abs(p.qty) * price * fee
            pnl_pct = sign * (price / p.entry_price - 1) * 100 - 2 * fee * 100
            trades.append({"symbol": s, "entry_day": p.entry_day.isoformat(), "exit_day": day.isoformat(),
                           "entry": p.entry_price, "exit": price, "pnl_pct": round(pnl_pct, 2),
                           "days": (day - p.entry_day).days})
            pending_exit.discard(s)
        for s in list(pending_entry):
            if s not in trading_today:
                continue
            pending_entry.remove(s)
            if s in positions or len(positions) >= system.max_positions:
                continue
            price = last_price[s]
            budget = min(equity() * system.position_pct / 100, cash if sign > 0 else equity())
            qty = budget / (price * (1 + fee))
            if qty <= 0:
                continue
            cash -= sign * qty * price + qty * price * fee
            positions[s] = Position(s, sign * qty, price, day)

        # 2. Decide on today's close, to fill at the next close.
        for s in trading_today:
            if s in positions:
                p = positions[s]
                move = sign * (last_price[s] / p.entry_price - 1) * 100
                if (any(market.check(c, s, day) for c in system.exit)
                        or (system.stop_loss_pct and move <= -system.stop_loss_pct)
                        or (system.take_profit_pct and move >= system.take_profit_pct)
                        or (system.max_hold_days and (day - p.entry_day).days >= system.max_hold_days)):
                    pending_exit.add(s)
            elif s not in pending_entry and all(market.check(c, s, day) for c in system.entry):
                pending_entry.append(s)
        curve.append((day, equity()))

    return {"curve": curve, "trades": trades, "open_positions": len(positions),
            "metrics": metrics(curve, trades, cash_start=initial),
            "benchmark": buy_and_hold(system.symbols, market, calendar)}


def buy_and_hold(symbols, market: Market, calendar) -> dict:
    """Equal amounts of each symbol bought on the first day and held to the end."""
    first, last = calendar[0], calendar[-1]
    rets = []
    for s in symbols:
        i0, i1 = market.index(s, first), market.index(s, last)
        if i0 is not None and i1 is not None:
            rets.append(market.data[s][i1][1] / market.data[s][i0][1] - 1)
    total = sum(rets) / len(rets) * 100 if rets else 0.0
    return {"total_return_pct": round(total, 2), "cagr_pct": round(_cagr(total, first, last), 2)}


def _cagr(total_pct, first, last):
    years = max((last - first).days / 365.25, 1 / 365.25)
    return ((1 + total_pct / 100) ** (1 / years) - 1) * 100 if total_pct > -100 else -100.0


def metrics(curve, trades, cash_start) -> dict:
    values = [v for _, v in curve]
    total = (values[-1] / cash_start - 1) * 100
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1) * 100)
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1 / 365.25)
    per_year = len(rets) / years if rets else 0
    sd = _std(rets) if len(rets) > 1 else 0
    sharpe = (sum(rets) / len(rets)) / sd * math.sqrt(per_year) if sd else 0.0
    wins = [t for t in trades if t["pnl_pct"] > 0]
    return {
        "total_return_pct": round(total, 2),
        "cagr_pct": round(_cagr(total, curve[0][0], curve[-1][0]), 2),
        "max_drawdown_pct": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_trade_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2) if trades else 0.0,
        "start": curve[0][0].isoformat(), "end": curve[-1][0].isoformat(),
    }


def signals(system: System, market: Market, day: date, held: set[str]) -> list[str]:
    """What the system says about `day`'s close, in words, for a trader's briefing."""
    out = []
    for s in system.symbols:
        if s not in market.data or market.index(s, day) is None:
            out.append(f"{s}: not enough price history to evaluate")
            continue
        if s in held:
            hits = [c for c in system.exit if market.check(c, s, day)]
            out.append(f"{s}: EXIT signal ({'; '.join(market.explain(c, s, day) for c in hits)})" if hits
                       else f"{s}: hold (no exit rule triggered)")
        else:
            ok = all(market.check(c, s, day) for c in system.entry)
            detail = "; ".join(market.explain(c, s, day) for c in system.entry)
            out.append(f"{s}: {'ENTRY signal (' + system.direction + ')' if ok else 'no entry'} ({detail})")
    return out
