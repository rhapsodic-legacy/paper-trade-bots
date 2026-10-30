"""The symbols the prototype trades, and starting prices for the offline random-walk feed."""
from .assets import Asset, AssetClass

STOCKS = ["AAPL", "MSFT", "NVDA", "KO", "JNJ", "GME", "SPY"]
CRYPTO = ["BTC", "ETH", "SOL", "DOGE"]
ASSETS = [Asset(s, AssetClass.STOCK) for s in STOCKS] + [Asset(s, AssetClass.CRYPTO) for s in CRYPTO]
START_PRICES = {"AAPL": 230, "MSFT": 420, "NVDA": 135, "KO": 70, "JNJ": 160, "GME": 25,
                "SPY": 580, "BTC": 68000, "ETH": 2600, "SOL": 150, "DOGE": 0.15}
