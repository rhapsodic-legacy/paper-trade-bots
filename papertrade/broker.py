from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Mapping

from .account import Account, Trade, money
from .assets import Asset


class Side(str, Enum):
    BUY = "buy"      # open or add to a long position
    SELL = "sell"    # reduce or close a long position
    SHORT = "short"  # open or add to a short position
    COVER = "cover"  # reduce or close a short position


class OrderRejected(Exception):
    pass


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: Decimal
    reason: str = ""  # the bot's explanation, kept for its journal


class Broker:
    """Fills market orders at the day's price and enforces the account rules.

    Rules:
      * Stocks trade whole shares on weekdays; crypto trades fractions every day.
      * Buys must be paid for in cash (no margin borrowing).
      * You can't buy while short or short while long: close the position first.
      * Shorting needs equity of at least `short_margin` of total short exposure
        after the trade (Reg T style 50% by default).
    """

    def __init__(self, assets: Mapping[str, Asset], fee_rate: Decimal = Decimal("0"),
                 short_margin: Decimal = Decimal("0.5"), ledger=None):
        self.assets = dict(assets)
        self.fee_rate = Decimal(fee_rate)
        self.short_margin = Decimal(short_margin)
        self.ledger = ledger

    def execute(self, account: Account, order: Order, day: date,
                prices: Mapping[str, Decimal]) -> Trade:
        asset = self.assets.get(order.symbol)
        if asset is None:
            raise OrderRejected(f"Unknown symbol {order.symbol}")
        if not asset.is_open(day):
            raise OrderRejected(f"{order.symbol} market is closed on {day:%A}")
        side = Side(order.side)
        qty = Decimal(order.quantity).quantize(asset.quantity_step, rounding=ROUND_DOWN)
        if qty <= 0:
            raise OrderRejected(f"Quantity must be at least {asset.quantity_step}")

        price = prices[order.symbol]
        fee = money(qty * price * self.fee_rate)
        held = account.position(order.symbol)

        if side is Side.BUY:
            if held.quantity < 0:
                raise OrderRejected(f"Cover your short in {order.symbol} before buying")
            if qty * price + fee > account.cash:
                raise OrderRejected(f"Not enough cash: need {money(qty * price + fee)}, have {money(account.cash)}")
            realized = -fee
        elif side is Side.SELL:
            if qty > held.quantity:
                raise OrderRejected(f"Can't sell {qty} {order.symbol}: you hold {max(held.quantity, 0)}")
            realized = (price - held.avg_price) * qty - fee
        elif side is Side.SHORT:
            if held.quantity > 0:
                raise OrderRejected(f"Sell your {order.symbol} before shorting it")
            exposure = account.short_exposure(prices) + qty * price
            equity_after = account.equity(prices) - fee
            if equity_after < exposure * self.short_margin:
                raise OrderRejected(f"Not enough margin: short exposure {money(exposure)} needs equity "
                                    f"{money(exposure * self.short_margin)}, have {money(equity_after)}")
            realized = -fee
        else:  # COVER
            if qty > -held.quantity:
                raise OrderRejected(f"Can't cover {qty} {order.symbol}: you are short {max(-held.quantity, 0)}")
            realized = (held.avg_price - price) * qty - fee

        trade = Trade(day, account.name, order.symbol, side.value, qty, price, fee,
                      money(realized), order.reason)
        account.apply(trade)
        if self.ledger is not None:
            self.ledger.record_trade(trade)
        return trade
