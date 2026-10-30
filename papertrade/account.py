from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Mapping

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


@dataclass
class Position:
    symbol: str
    quantity: Decimal = Decimal("0")  # negative means short
    avg_price: Decimal = Decimal("0")

    def market_value(self, price: Decimal) -> Decimal:
        return self.quantity * price

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        return (price - self.avg_price) * self.quantity


@dataclass(frozen=True)
class Trade:
    day: date
    account: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    reason: str = ""


@dataclass
class Account:
    name: str
    starting_cash: Decimal = Decimal("100000")
    cash: Decimal = None
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    trades: list[Trade] = field(default_factory=list)

    def __post_init__(self):
        self.starting_cash = Decimal(self.starting_cash)
        if self.cash is None:
            self.cash = self.starting_cash

    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position(symbol)

    def equity(self, prices: Mapping[str, Decimal]) -> Decimal:
        return money(self.cash + sum(p.market_value(prices[s]) for s, p in self.positions.items()))

    def short_exposure(self, prices: Mapping[str, Decimal]) -> Decimal:
        return sum(-p.market_value(prices[s]) for s, p in self.positions.items() if p.quantity < 0)

    def return_pct(self, prices: Mapping[str, Decimal]) -> Decimal:
        return ((self.equity(prices) / self.starting_cash) - 1) * 100

    def apply(self, trade: Trade) -> None:
        """Update cash and positions for a filled trade. Validation lives in Broker."""
        signed = trade.quantity if trade.side in ("buy", "cover") else -trade.quantity
        pos = self.position(trade.symbol)
        self.cash -= signed * trade.price + trade.fee
        new_qty = pos.quantity + signed
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            # Opening or adding: blend the average price.
            pos.avg_price = (pos.avg_price * abs(pos.quantity) + trade.price * trade.quantity) / abs(new_qty)
        pos.quantity = new_qty
        self.realized_pnl += trade.realized_pnl
        if new_qty == 0:
            self.positions.pop(trade.symbol, None)
        else:
            self.positions[trade.symbol] = pos
        self.trades.append(trade)
