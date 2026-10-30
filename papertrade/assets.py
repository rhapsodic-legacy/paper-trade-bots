from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class Asset:
    symbol: str
    asset_class: AssetClass

    @property
    def quantity_step(self) -> Decimal:
        """Smallest tradable quantity: whole shares for stocks, satoshis for crypto."""
        return Decimal("1") if self.asset_class is AssetClass.STOCK else Decimal("0.00000001")

    def is_open(self, day: date) -> bool:
        """Stocks trade on weekdays; crypto trades every day."""
        return self.asset_class is AssetClass.CRYPTO or day.weekday() < 5
