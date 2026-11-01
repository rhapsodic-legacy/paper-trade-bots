from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Mapping

from .account import Account, Trade, money
from .assets import Asset
from .broker import Broker, Order, OrderRejected
from .ledger import Ledger
from .prices import PriceFeed
from .standing import PROTECTIVE, StandingOrder, StandingOrders

BENCHMARK = "SPY"  # every bot is measured against simply buying the S&P 500 and holding it


@dataclass(frozen=True)
class MarketView:
    """What a bot is allowed to see when it decides what to trade."""
    day: date
    prices: Mapping[str, Decimal]
    open_symbols: tuple[str, ...]
    assets: Mapping[str, Asset]
    account: Account
    feed: PriceFeed
    notes: str = ""  # benchmark comparison, standing orders and theses, for the briefing

    def history(self, symbol: str, n: int) -> list[Decimal]:
        return self.feed.history(symbol, self.day, n)

    @property
    def equity(self) -> Decimal:
        return self.account.equity(self.prices)


Strategy = Callable[[MarketView], list[Order | StandingOrder]]


@dataclass
class DayResult:
    day: date
    fills: list[Trade] = field(default_factory=list)
    rejections: list[tuple[str, Order, str]] = field(default_factory=list)
    triggered: list[Trade] = field(default_factory=list)  # standing orders that fired at this close
    standing: list[tuple[str, bool, str]] = field(default_factory=list)  # (bot, placed ok?, message)


class Simulation:
    def __init__(self, assets: list[Asset], feed: PriceFeed, ledger: Ledger | None = None,
                 starting_cash: Decimal = Decimal("100000"), fee_rate: Decimal = Decimal("0")):
        self.assets = {a.symbol: a for a in assets}
        self.feed = feed
        self.ledger = ledger or Ledger()
        self.starting_cash = Decimal(starting_cash)
        self.broker = Broker(self.assets, fee_rate=fee_rate, ledger=self.ledger)
        self.standing = StandingOrders(self.ledger.db)
        # Resume any accounts already in the ledger.
        self.accounts = {n: self.ledger.load_account(n) for n in self.ledger.account_names()}

    def add_account(self, name: str) -> Account:
        if name not in self.accounts:
            account = Account(name, self.starting_cash)
            self.ledger.add_account(account)
            self.accounts[name] = account
        return self.accounts[name]

    def view(self, day: date, account: Account, prices: Mapping[str, Decimal] | None = None) -> MarketView:
        prices = self.feed.prices(day) if prices is None else prices
        open_symbols = tuple(s for s, a in self.assets.items() if a.is_open(day) and s in prices)
        notes = "\n".join(n for n in (self.benchmark_note(account, prices), self.standing.notes(account, day)) if n)
        return MarketView(day, prices, open_symbols, self.assets, account, self.feed, notes)

    def benchmark_return(self, account: Account, prices: Mapping[str, Decimal]) -> tuple[date, Decimal] | None:
        """(first day, % return) of buying SPY with all the cash on the account's first day and holding."""
        snaps = self.ledger.snapshots(account.name)
        if not snaps or BENCHMARK not in prices:
            return None
        first = snaps[0][0]
        start = self.ledger.marks(first).get(BENCHMARK)
        if not start:
            return None
        return first, (prices[BENCHMARK] / start - 1) * 100

    def benchmark_note(self, account: Account, prices: Mapping[str, Decimal]) -> str:
        bench = self.benchmark_return(account, prices)
        if bench is None:
            return ""
        first, pct = bench
        return (f"Benchmark: simply buying the S&P 500 (SPY) on your first day ({first:%d %b}) and holding "
                f"it would be {pct:+.2f}%. You are {account.return_pct(prices):+.2f}%.")

    def step(self, day: date, strategies: Mapping[str, Strategy]) -> DayResult:
        """Run one trading day: each bot sees the market, places orders, then accounts are marked to market."""
        prices = self.feed.prices(day)
        result = DayResult(day)
        for name in strategies:
            self.add_account(name)
        # Standing orders placed earlier fire at this close before anyone decides, so each bot sees
        # what its stops did today.
        open_symbols = {s for s, a in self.assets.items() if a.is_open(day) and s in prices}
        for name, account in self.accounts.items():
            for placed, order in self.standing.due(account, day, prices, open_symbols):
                try:
                    trade = self.broker.execute(account, order, day, prices)
                except OrderRejected as e:
                    self.standing.close(placed.id, "failed", day, f"{placed.describe()} triggered but failed: {e}")
                    continue
                result.triggered.append(trade)
                self.standing.close(placed.id, "filled", day,
                                    f"{placed.describe()} fired: {trade.side} {trade.quantity.normalize()} "
                                    f"at ${trade.price:,}")
        # Every bot decides first, then orders are filled. If deciding fails part-way (say Claude
        # becomes unavailable), no bot's own orders have been traded yet, so the day can be run again.
        decisions = [(name, list(strategy(self.view(day, self.accounts[name], prices))))
                     for name, strategy in strategies.items()]
        for name, orders in decisions:
            account = self.accounts[name]
            for order in (o for o in orders if isinstance(o, Order)):
                try:
                    trade = self.broker.execute(account, order, day, prices)
                except OrderRejected as e:
                    result.rejections.append((name, order, str(e)))
                    continue
                result.fills.append(trade)
                if trade.side in ("buy", "short"):
                    self.standing.record_thesis(name, trade.symbol, day, trade.side, order.invalidation)
            # Standing orders go in after today's fills, so a bot can buy and set its stop in one go.
            for o in (o for o in orders if isinstance(o, StandingOrder)):
                problem = self._check_standing(account, o, prices)
                if problem:
                    result.standing.append((name, False, f"{o.kind.replace('_', ' ')} on {o.symbol}: {problem}"))
                else:
                    result.standing.append((name, True, self.standing.place(name, day, o)))
        self.ledger.record_marks(day, prices)
        for name, account in self.accounts.items():
            self.ledger.record_snapshot(day, name, money(account.cash), account.equity(prices))
        return result

    def _check_standing(self, account: Account, o: StandingOrder, prices: Mapping[str, Decimal]) -> str:
        if o.symbol not in self.assets or o.symbol not in prices:
            return "unknown symbol or no price"
        if o.price <= 0:
            return ""  # a cancel
        close, held = prices[o.symbol], account.position(o.symbol).quantity
        if o.kind in PROTECTIVE:
            if held == 0:
                return "you have no position to protect"
            long = held > 0
            if o.kind == "stop_loss" and (o.price >= close if long else o.price <= close):
                return f"the stop at ${o.price:,} is already past today's close of ${close:,}; trade now instead"
            if o.kind == "take_profit" and (o.price <= close if long else o.price >= close):
                return f"the target at ${o.price:,} is already past today's close of ${close:,}; trade now instead"
        elif o.kind == "limit_buy" and o.price >= close:
            return f"${o.price:,} is at or above today's close of ${close:,}; just buy instead"
        elif o.kind == "limit_short" and o.price <= close:
            return f"${o.price:,} is at or below today's close of ${close:,}; just short instead"
        if o.kind not in PROTECTIVE and o.unit == "all":
            return "a limit order needs an amount in usd or units"
        return ""

    def leaderboard(self, day: date) -> list[tuple[str, Decimal, Decimal]]:
        """(name, equity, return %) sorted best first."""
        prices = self.feed.prices(day)
        rows = [(n, a.equity(prices), a.return_pct(prices).quantize(Decimal("0.01")))
                for n, a in self.accounts.items()]
        return sorted(rows, key=lambda r: r[1], reverse=True)
