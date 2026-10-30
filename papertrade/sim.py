from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Mapping

from .account import Account, Trade, money
from .assets import Asset
from .broker import Broker, Order, OrderRejected
from .ledger import Ledger
from .prices import PriceFeed


@dataclass(frozen=True)
class MarketView:
    """What a bot is allowed to see when it decides what to trade."""
    day: date
    prices: Mapping[str, Decimal]
    open_symbols: tuple[str, ...]
    assets: Mapping[str, Asset]
    account: Account
    feed: PriceFeed

    def history(self, symbol: str, n: int) -> list[Decimal]:
        return self.feed.history(symbol, self.day, n)

    @property
    def equity(self) -> Decimal:
        return self.account.equity(self.prices)


Strategy = Callable[[MarketView], list[Order]]


@dataclass
class DayResult:
    day: date
    fills: list[Trade] = field(default_factory=list)
    rejections: list[tuple[str, Order, str]] = field(default_factory=list)


class Simulation:
    def __init__(self, assets: list[Asset], feed: PriceFeed, ledger: Ledger | None = None,
                 starting_cash: Decimal = Decimal("100000"), fee_rate: Decimal = Decimal("0")):
        self.assets = {a.symbol: a for a in assets}
        self.feed = feed
        self.ledger = ledger or Ledger()
        self.starting_cash = Decimal(starting_cash)
        self.broker = Broker(self.assets, fee_rate=fee_rate, ledger=self.ledger)
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
        return MarketView(day, prices, open_symbols, self.assets, account, self.feed)

    def step(self, day: date, strategies: Mapping[str, Strategy]) -> DayResult:
        """Run one trading day: each bot sees the market, places orders, then accounts are marked to market."""
        prices = self.feed.prices(day)
        result = DayResult(day)
        # Every bot decides first, then orders are filled. If deciding fails part-way (say Claude
        # becomes unavailable), nothing has been traded yet, so the day can simply be run again.
        decisions = [(name, list(strategy(self.view(day, self.add_account(name), prices))))
                     for name, strategy in strategies.items()]
        for name, orders in decisions:
            account = self.accounts[name]
            for order in orders:
                try:
                    result.fills.append(self.broker.execute(account, order, day, prices))
                except OrderRejected as e:
                    result.rejections.append((name, order, str(e)))
        self.ledger.record_marks(day, prices)
        for name, account in self.accounts.items():
            self.ledger.record_snapshot(day, name, money(account.cash), account.equity(prices))
        return result

    def leaderboard(self, day: date) -> list[tuple[str, Decimal, Decimal]]:
        """(name, equity, return %) sorted best first."""
        prices = self.feed.prices(day)
        rows = [(n, a.equity(prices), a.return_pct(prices).quantize(Decimal("0.01")))
                for n, a in self.accounts.items()]
        return sorted(rows, key=lambda r: r[1], reverse=True)
