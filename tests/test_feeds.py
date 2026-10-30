"""Feed tests use small hand-written payloads in the shape Yahoo and RSS return.

They are test fixtures, not real market data.
"""
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal as D

from papertrade import Account, Asset, AssetClass, HistoricalPriceFeed, Simulation
from papertrade.briefing import build_briefing, pct_change
from papertrade.market_data import PriceCache, parse_chart, yahoo_ticker
from papertrade.news import MARKET, NewsCache, NewsItem, parse_rss

AAPL, BTC = Asset("AAPL", AssetClass.STOCK), Asset("BTC", AssetClass.CRYPTO)


def ts(y, m, d, h=13, mi=30):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


def chart(timestamps, closes, offset=-14400):
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"gmtoffset": offset}, "timestamp": timestamps,
        "indicators": {"quote": [{"close": closes}]}}]}}).encode()


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Test headline one &amp; more</title><link>https://example.com/1</link>
  <pubDate>Fri, 23 Oct 2026 14:00:00 GMT</pubDate><description>&lt;b&gt;Bold&lt;/b&gt; summary</description></item>
<item><title>Test headline two</title><link>https://example.com/2</link>
  <pubDate>Mon, 26 Oct 2026 09:00:00 GMT</pubDate><source url="x">Wire</source></item>
<item><title>No date, skipped</title><link>https://example.com/3</link></item>
</channel></rss>"""


class PriceDataTest(unittest.TestCase):
    def test_ticker_mapping(self):
        self.assertEqual(yahoo_ticker(AAPL), "AAPL")
        self.assertEqual(yahoo_ticker(BTC), "BTC-USD")

    def test_parse_chart_skips_gaps_and_uses_exchange_date(self):
        rows = parse_chart(chart([ts(2026, 10, 22), ts(2026, 10, 23), ts(2026, 10, 24)], [101.234, None, 99.0]))
        self.assertEqual(rows, [(date(2026, 10, 22), D("101.23")), (date(2026, 10, 24), D("99.00"))])

    def test_parse_chart_error(self):
        payload = json.dumps({"chart": {"result": None, "error": {"description": "No data found"}}}).encode()
        with self.assertRaisesRegex(ValueError, "No data"):
            parse_chart(payload)

    def test_cache_update_and_feed(self):
        def fake_fetch(url):
            if "BTC-USD" in url:
                raise OSError("offline")
            return chart([ts(2026, 10, 22), ts(2026, 10, 23)], [100.0, 102.0])
        cache = PriceCache(fetch=fake_fetch)
        errors = cache.update([AAPL, BTC], date(2026, 10, 1), date(2026, 10, 23))
        self.assertEqual(list(errors), ["BTC"])
        feed = cache.feed()
        self.assertEqual(feed.prices(date(2026, 10, 25)), {"AAPL": D("102.00")})
        # Updating again overwrites rather than duplicating.
        cache.update([AAPL], date(2026, 10, 1), date(2026, 10, 23))
        self.assertEqual(len(cache.feed().history("AAPL", date(2026, 10, 25), 10)), 2)


class NewsTest(unittest.TestCase):
    def test_parse_rss(self):
        items = parse_rss(RSS, "AAPL", "Yahoo Finance")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Test headline one & more")
        self.assertEqual(items[0].summary, "Bold summary")
        self.assertEqual(items[1].source, "Wire")

    def test_option_quotes_dropped_and_names_searched(self):
        from papertrade.news import news_urls
        rss = RSS.replace(b"Test headline two", b"GME Nov 2026 25.000 call (GME261120C00025000)")
        self.assertEqual([i.title for i in parse_rss(rss, "GME", "Yahoo Finance")], ["Test headline one & more"])
        google = news_urls(Asset("KO", AssetClass.STOCK))[1][0]
        self.assertIn("Coca-Cola", google)
        self.assertNotIn("q=KO", google)

    def test_no_lookahead_and_dedupe(self):
        cache = NewsCache(fetch=lambda url: RSS)
        cache.update([AAPL])
        got = cache.headlines(date(2026, 10, 23), ["AAPL"])
        self.assertEqual([i.title for i in got], ["Test headline one & more"])  # the 26 Oct story is in the future
        self.assertEqual(len(cache.headlines(date(2026, 10, 26), ["AAPL"], lookback_days=5)), 2)

    def test_market_wide_news_included(self):
        cache = NewsCache(fetch=lambda url: b"<rss/>")
        cache.add([NewsItem(datetime(2026, 10, 23, 12, tzinfo=timezone.utc), MARKET, "Fed holds rates", "Wire",
                            "https://example.com/fed")])
        self.assertEqual([i.symbol for i in cache.headlines(date(2026, 10, 23), ["AAPL"])], [MARKET])


class BriefingTest(unittest.TestCase):
    def test_pct_change(self):
        self.assertEqual(pct_change([D("100"), D("110")], 1), D("10"))
        self.assertIsNone(pct_change([D("100")], 1))

    def test_briefing_contents(self):
        feed = HistoricalPriceFeed([(date(2026, 10, 22), "AAPL", "100"), (date(2026, 10, 23), "AAPL", "110"),
                                    (date(2026, 10, 24), "BTC", "50000")])
        sim = Simulation([AAPL, BTC], feed)
        acct = sim.add_account("Bob")
        news = NewsCache(fetch=lambda url: RSS)
        news.update([AAPL])
        text = build_briefing(sim.view(date(2026, 10, 24), acct), ["AAPL", "BTC"], news,
                              ["Not enough cash to buy 3 BTC"]).text
        self.assertIn("Saturday 24 October 2026", text)
        self.assertIn("Closed today (you can't trade these): AAPL", text)
        self.assertIn("+10.0%", text)
        self.assertIn("[AAPL] Test headline one & more", text)
        self.assertNotIn("Test headline two", text)
        self.assertIn("Not enough cash to buy 3 BTC", text)


if __name__ == "__main__":
    unittest.main()
