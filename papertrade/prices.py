import csv
import random
from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Protocol

from .assets import Asset, AssetClass


class PriceFeed(Protocol):
    def prices(self, day: date) -> dict[str, Decimal]:
        """Closing price of every symbol on `day` (last close if its market was shut)."""

    def history(self, symbol: str, day: date, n: int) -> list[Decimal]:
        """The last `n` closes for `symbol` up to and including `day`, oldest first."""


class HistoricalPriceFeed:
    """Replays closing prices from rows of (date, symbol, close).

    Missing days (weekends, holidays) carry the last known close forward.
    """

    def __init__(self, rows: Iterable[tuple[date, str, Decimal]]):
        self._by_symbol: dict[str, list[tuple[date, Decimal]]] = {}
        for day, symbol, close in rows:
            self._by_symbol.setdefault(symbol, []).append((day, Decimal(str(close))))
        for series in self._by_symbol.values():
            series.sort()

    @classmethod
    def from_csv(cls, path) -> "HistoricalPriceFeed":
        with open(path, newline="") as f:
            return cls((date.fromisoformat(r["date"]), r["symbol"], r["close"])
                       for r in csv.DictReader(f))

    def prices(self, day: date) -> dict[str, Decimal]:
        out = {}
        for symbol, series in self._by_symbol.items():
            i = bisect_right(series, (day, Decimal("Infinity")))
            if i:
                out[symbol] = series[i - 1][1]
        return out

    def history(self, symbol: str, day: date, n: int) -> list[Decimal]:
        """The last `n` closes for `symbol` up to and including `day`, oldest first."""
        return [close for _, close in self.bars(symbol, day, n)]

    def bars(self, symbol: str, day: date, n: int) -> list[tuple[date, Decimal]]:
        """The last `n` (date, close) pairs for `symbol` up to and including `day`, oldest first."""
        series = self._by_symbol.get(symbol, [])
        i = bisect_right(series, (day, Decimal("Infinity")))
        return series[max(0, i - n):i]


class SyntheticPriceFeed(HistoricalPriceFeed):
    """Random-walk prices, so the sim runs offline before real data is wired in."""

    def __init__(self, assets: Iterable[Asset], start_prices: dict[str, float], start: date,
                 days: int, seed: int = 0, daily_vol: dict[AssetClass, float] | None = None):
        vol = daily_vol or {AssetClass.STOCK: 0.015, AssetClass.CRYPTO: 0.035}
        rows = []
        for asset in assets:
            # One generator per symbol, so asking for more days never changes the earlier prices.
            rng = random.Random(f"{seed}-{asset.symbol}")
            price = start_prices[asset.symbol]
            for i in range(days):
                day = start + timedelta(days=i)
                if not asset.is_open(day):
                    continue
                price *= 1 + rng.gauss(0.0003, vol[asset.asset_class])
                rows.append((day, asset.symbol, Decimal(f"{price:.2f}" if price >= 1 else f"{price:.6f}")))
        super().__init__(rows)
