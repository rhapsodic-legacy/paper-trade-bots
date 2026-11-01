# Paper_Trade

**Latest results:** [results/README.md](results/README.md) (leaderboard, today's trades and journals, updated after each daily run).

A paper-money trading simulator for AI bots with personalities. The whole prototype plan is here: the market engine, real data feeds, seven Claude-driven bots, journals that act as
their memory, a dashboard, a research desk of analyst bots, and a strategy lab where each bot designs and
tests its own trading system on historical data. The engine and feeds
need only Python 3.10 or newer. Everything runs on free data (no keys) and your Claude
subscription, with no Anthropic API key.

## Setup (once)

1. Install Python 3.10 or newer.
2. Install Claude Code (claude.com/claude-code), run `claude` in a terminal, and sign in with
   `/login` using your Claude Max (or Pro) account. The bots call Claude through Claude Code, so
   their thinking counts toward your plan's usage limits instead of costing extra.
3. That's it. Optionally, `pip install -r requirements.txt` adds the Anthropic SDK, only needed
   if you ever want to use an API key instead (`--backend api`).

```
python3 demo.py                 # two simulated weeks with all seven bots, then a sample briefing
python3 demo.py demo.db         # same, saving the ledger to a SQLite file
python3 fetch_data.py           # download real prices and today's headlines into data.db
python3 run_bots.py             # the seven Claude bots trade today on that real data
python3 run_bots.py --synthetic --days 5          # or five days on offline random prices
python3 run_bots.py --synthetic --show-prompt "Contrarian Carl"   # see exactly what Claude reads
python3 journal_feed.py --ledger synthetic.db     # read every bot's journal feed, newest first
python3 journal_feed.py --markdown journals       # or write one Markdown file per bot
python3 dashboard.py --ledger synthetic.db        # build dashboard.html and open it
python3 import_history.py --csv path/to/kaggle/   # load long price history (or --yahoo --years 10)
python3 build_system.py --all                     # every bot designs and tests a trading system
python3 build_system.py --list                    # see each bot's system and lab notebook
python3 -m unittest -v         # run the tests
```

### Run it every day automatically

`daily_run.py` does the whole daily routine (fetch data, run the desks and bots, build the
dashboard) and can schedule itself on a Mac:

```
python3 daily_run.py                    # run today's routine now
python3 daily_run.py --install          # run it every day at 17:00 local time (or --time 16:30)
python3 daily_run.py --status           # the schedule and the last week of runs
python3 daily_run.py --uninstall        # stop
```

It runs every day, not just weekdays, because Bitcoin Bob trades on weekends; the stock bots
skip days the market is closed. If the Mac is asleep at that time the run happens when it
wakes; if it is shut down, that day is missed. macOS won't let a scheduled job read
Desktop, Documents or Downloads, so keep the folder somewhere like `~/paper_trade`.
Each run writes a log to `logs/` and adds a line to `logs/runs.csv`; a failed run shows a
notification.

### Publishing results to GitHub

When this folder is a clone of the GitHub repo, each daily run first pulls code updates
(`git pull --ff-only`) and finishes by running `publish_results.py`, which commits and pushes:

- `results/README.md`: leaderboard, the day's trades with each bot's reason, journal entries, desk headlines
- `results/latest.json` and `results/days/DATE.json`: the same data in full, for tools (and Claude) to read
- `docs/index.html`: the dashboard. Turn on GitHub Pages (Settings, Pages, branch `main`, folder `/docs`) to open it from anywhere.

The ledger and caches (`*.db`) stay on the computer that runs the bots.

## What's here

| File | What it does |
| --- | --- |
| `papertrade/assets.py` | Stocks (whole shares, weekdays) and crypto (fractions, every day) |
| `papertrade/account.py` | Cash, positions, average cost, realized and unrealized P&L, equity |
| `papertrade/broker.py` | Fills orders and enforces the rules below |
| `papertrade/prices.py` | Price feeds: replay a CSV of real closes, or a seeded random walk for offline testing |
| `papertrade/ledger.py` | SQLite record of accounts, every trade (with the bot's reason) and daily equity |
| `papertrade/sim.py` | Runs a day: each bot sees a `MarketView`, returns orders, accounts are marked to market |
| `papertrade/market_data.py` | Real daily closes for stocks and crypto from Yahoo Finance, cached in SQLite |
| `papertrade/news.py` | Headlines from Yahoo Finance and Google News RSS, per symbol and market-wide, cached in SQLite |
| `papertrade/briefing.py` | Builds each bot's daily briefing: account, prices with 1/5/20-day moves, news, rejected orders |
| `papertrade/personas.py` | The seven personalities: character, watchlist, what they may trade, who may short |
| `papertrade/brain.py` | Sends each bot's briefing to Claude and turns the answer into checked orders and a journal entry |
| `papertrade/journal.py` | Weekly and monthly summaries, the memory each bot reads every morning, and the journal feed |
| `papertrade/desk.py` | The research desk: five analyst bots, their daily reports and their track records |
| `papertrade/history.py` | Long price histories from Kaggle-style CSVs or Yahoo, stored in `history.db` |
| `papertrade/strategy.py` | The rule language bots write systems in, its indicators, and the backtester |
| `papertrade/lab.py` | The strategy lab: design, refine on training years, test once on unseen years |
| `papertrade/universe.py` | The symbols traded: 7 stocks and ETFs, 4 coins |
| `papertrade/standing.py` | Stop-loss, take-profit and limit orders checked at every close, and each trade's invalidation |
| `papertrade/events.py` | The market calendar: earnings dates, Fed decisions, CPI and jobs reports |
| `papertrade/filings.py` | The filings clerk: SEC filings and insider trades for the watched stocks |
| `papertrade/mining.py` | Monthly journal mining: which kinds of reasoning won and which lost |
| `daily_run.py`, `publish_results.py` | The scheduled daily routine, and pushing results to GitHub |
| `run_bots.py` | Runs the Claude bots for a day (or several), saving trades and journals |
| `papertrade/report.py`, `papertrade/templates/dashboard.html` | Gathers the dashboard's data and the page it fills |
| `dashboard.py` | Builds a single self-contained `dashboard.html` from a ledger |
| `import_history.py` | Loads historical prices for the lab |
| `build_system.py` | Sends bots to the strategy lab |
| `journal_feed.py` | Prints each bot's journal feed, or writes it out as Markdown |
| `demo.py` | Placeholder rule-based bots to exercise the engine without Claude |
| `fetch_data.py` | Fills `data.db` with real data and prints a sample briefing |

## Trading rules

- Every bot starts with $100,000 in cash.
- Orders fill at the day's closing price. Fees are off by default (`fee_rate` turns them on).
- No borrowing to buy: a buy must be paid for in cash.
- Shorting is allowed (for Contrarian Carl), with a 50% margin requirement: equity must be
  at least half of total short exposure after the trade.
- A position is either long or short. Close one before opening the other.
- Rejected orders don't stop the day. They come back in `DayResult.rejections` with the reason,
  which the bot can learn from in its journal.

### Standing orders and "what would prove me wrong"

A stop-loss written in a journal is a wish. `papertrade/standing.py` makes it an order:

- `stop_loss` and `take_profit` close a position when the close moves past their price;
  `limit_buy` and `limit_short` open one when the close reaches theirs (they expire after 10 days
  unless the bot says otherwise).
- They're checked at every daily close, before the bots decide and even on days a bot isn't
  called. Only closes are known, so an order fills at the close that crosses its price, which can
  be worse than the price set after a gap. Fired orders show up in the results marked `[stop-loss ...]`.
- Orders that make no sense (a stop above today's close on a long, a limit buy above the price)
  are refused with the reason, which the bot sees the next day.
- Every buy and short carries an invalidation: one sentence on what would prove it wrong. It's
  shown to the bot every day while it holds the position, with a nudge to act or explain.

### The benchmark

Every bot is measured against putting all $100,000 into SPY (the S&P 500) on its first day and
doing nothing. It's in every briefing, on the leaderboard and chart, and in `results/README.md`.
Most active traders, human or AI, don't beat it; that is one of the lessons.

## Plugging in a bot

A bot is any function `view -> list[Order | StandingOrder]`. `view` has the day, prices, which
markets are open, the bot's own account, `view.history(symbol, n)`, and `view.notes` (its
benchmark comparison, standing orders and theses). Each `Order` carries a `reason` string that is
saved with the trade and an optional `invalidation`; a `StandingOrder` places or cancels a stop,
target or limit.

## Data feeds

- Prices come from Yahoo Finance's free chart endpoint. It covers stocks and crypto (as `BTC-USD`),
  needs no API key and can fetch years of history. It's unofficial, so it may rate-limit or change.
- News comes from RSS, which only carries the last day or two. Run `fetch_data.py` daily and the
  archive in `data.db` grows, so later runs can replay the bots against real past news.
- Briefings never include news published after the day being simulated, so bots can't peek at the future.
- A failed fetch is reported and skipped; the bots just see "no data" or "no headlines" for that item.
- The market calendar (`papertrade/events.py`) adds earnings dates for the watched stocks from
  Nasdaq's earnings calendar, Fed rate decisions from the Federal Reserve's page, and CPI and jobs
  report dates from the Bureau of Labor Statistics. The published Fed and BLS dates through 2027
  are built in, so the calendar still works when a government site turns a script away.
- The filings clerk (`papertrade/filings.py`) reads SEC EDGAR for the watched stocks: 8-Ks in
  plain words ("quarterly results", "executive or director change"), quarterly and annual reports,
  big-stake filings, and insider trades from Form 4, read down to who bought or sold how many
  shares and whether it was a pre-set 10b5-1 plan. The SEC asks scripts to identify themselves:
  set `PAPERTRADE_SEC_CONTACT` to your name and email. Each filing is downloaded once, ever.
- Both are fetched once a day by `fetch_data.py` into `data.db` and read from there by every bot
  and research desk, so adding bots doesn't add requests.

## The bots

| Bot | Style | Trades |
| --- | --- | --- |
| Cautious Retiree | Dividends, index funds, big cash cushion | Stocks |
| Meme-Stock Gambler | Hype, big single bets | Stocks |
| Value Investor | Buys good companies after they fall | Stocks |
| Momentum Trader | Buys what's rising, cuts losers fast | Stocks |
| Tech Growth Bull | Big tech and chips, buys dips | Stocks |
| Contrarian Carl | Bets against the crowd, shorts rallies | Stocks, can short |
| Bitcoin Bob | Crypto only, Bitcoin first | Crypto |

Each day a bot gets its personality and the shared rules as a system prompt, and its briefing as
the message. Claude (Opus via Claude Code, at medium effort) answers in a fixed JSON shape: a one-line market
view, a list of orders (in dollars, units, or "all"), and a journal entry in the bot's voice.

Guardrails between Claude and the broker:
- Orders for symbols off the bot's watchlist, the wrong asset class (stocks for Bob), closed
  markets, or shorts by bots that don't short are dropped before they reach the broker.
- Dropped and rejected orders appear in the next day's briefing, so the bot can learn from them.
- An API error, a refusal or an unreadable answer means the bot holds that day; the reason is
  saved with its journal. If Claude Code isn't installed or signed in, or your plan's usage limit
  is reached, the run stops before anything is recorded; run it again later and it carries on.

## Journals and memory

- **Daily:** every day a bot trades, it writes a journal entry in its own voice. Stock-only bots
  rest on weekends (no API call); Bitcoin Bob trades every day.
- **Weekly and monthly:** when a week (Monday to Sunday) or a month ends, the next run rolls it up.
  The numbers (value at start and end, return, every trade, best and worst trade) come straight
  from the ledger; Claude writes the story and picks 1 to 4 lessons to keep. Monthly summaries are
  built from that month's weekly summaries.
- **Memory:** each morning's briefing includes the lessons the bot chose to keep, its last three
  monthly and weekly summaries, and its last five daily entries. It never includes anything dated
  after the day being traded.
- **If Claude is unavailable** when a summary is due, the facts are saved on their own and the
  summary is retried on the next run.
- **Feed:** `journal_feed.py` shows everything a bot has written, newest first, ready for the
  dashboard in step 5.

Keeping the facts in code and only the narrative in Claude is deliberate: a bot can misremember
how it felt, but never how much money it made.

## Journal mining

Once a month (on the first run after a month ends), `papertrade/mining.py` turns every closed
trade into a round trip: why it was opened, what the bot said would prove it wrong, why it was
closed, and what it made. Positions still open are included at their month-end value. Claude
labels each opening reason with one category (trend, dip-buying, valuation, news, desk call,
macro, trading-system signal, gut feel) and the win rate and profit per category are counted in
code. The scoreboard and a few lessons per bot go into each bot's memory, and the full report is
published as `results/lessons/YYYY-MM.md`.

## Research desk

Five analyst bots each cover one beat, so traders don't all read the same news:

| Desk | Covers | Extra news searches |
| --- | --- | --- |
| Tech & AI | AAPL, MSFT, NVDA | AI chip demand, big tech earnings |
| Consumer & Health | KO, JNJ | consumer staples, pharma |
| Retail Sentiment | GME | meme stocks and retail traders |
| Macro & Energy | SPY, plus market-wide news | oil prices, inflation, the Fed |
| Crypto | BTC, ETH, SOL, DOGE | crypto regulation, bitcoin ETF flows |

Each trading day, before the traders run, every desk with fresh headlines makes one Claude call
and publishes a report: a headline, a summary, the house view, the strongest dissent it can
argue, a 5-day call (bullish, bearish or neutral, with confidence) on each symbol it covers, and
the headlines it relied on. Traders then get the reports for the desks covering their watchlist
in place of raw headlines. A trader with no report that day falls back to headlines.

Every call is scored five trading days later against what the price actually did, and the score
travels with the report ("7 of 12 calls right"), so traders and people watching can judge whether
a desk is worth listening to. Desks also see their own record when writing.

The news is read once per topic instead of once per trader, but the seven trader calls still
happen, so the Claude usage saving depends on how much news there is. The bigger win is on the
news feeds themselves: they're fetched once into a shared cache, however many bots read them.

## Strategy lab

**History.** `import_history.py` reads Kaggle downloads (one file per ticker like `aapl.us.txt`,
combined files like `all_stocks_5yr.csv`, or crypto files like `coin_Bitcoin.csv`), Yahoo-style
CSVs, or downloads up to decades of adjusted daily prices from Yahoo. Adjusted close is used when
available, so splits and dividends don't look like crashes. `--synthetic` makes clearly fake
random-walk data for trying the lab offline. Kaggle needs a free account; download the files
yourself and point the script at them.

**Designing a system.** A bot never writes code that gets run. It writes its system in a small
rule language: indicators (moving averages, RSI, N-day return, volatility, z-score, breakouts,
drawdown, volume), conditions like `rsi:14 < 30` or `price crosses_above sma:50`, cross-market
filters like `SPY@price > SPY@sma:200`, plus position size, stops, take-profit and a maximum hold.
Mistakes come back as plain-English errors the bot can fix next round.

**Testing honestly.** History is split: roughly the first 70% is training, the last 30% is held
back. The bot designs on the training years, sees its results next to buy-and-hold, and refines
over a few rounds (default 3). The final version is then run once on the held-back years it has
never seen. Trades are decided on one close and filled at the next, every fill pays a 0.1% fee,
and the bot never sees test-period numbers while designing. Afterwards it writes a lab notebook
entry in its own voice comparing training and test results, and rates how far it trusts the
system (1 to 5).

**Using it.** The system's daily signals go into the bot's briefing as advice, alongside its
test-period results and trust rating; the bot still decides. The dashboard shows each bot's
system, training versus unseen-test results against buy-and-hold, and its notebook. Running the
lab again replaces the old system.

Expect most systems to look better on training years than on test years. That gap is overfitting,
and seeing it happen is one of the most useful lessons the project can teach.

## Dashboard

`python3 dashboard.py` writes one HTML file with no outside dependencies, so it opens straight
from disk and can be emailed. It shows the leaderboard (click a row to jump to that bot), every
bot's account value over time (hover for exact values on any day), and for the chosen bot its
holdings, recent trades with the reasons it gave, and its journal feed with weekly and monthly
summaries, plus each desk's latest report, calls and track record. It follows your light or dark system setting and works on a phone. Re-run it after each
trading day to refresh. `dashboard_demo.html` is a sample built from `demo.py`'s rule-based bots.

Usage: a trading day is about 12 Claude calls (7 traders, up to 5 desks), each taking 10 to 60
seconds through Claude Code, plus one call per bot when a weekly or monthly summary is due. The
strategy lab uses 4 calls per bot. All of it comes out of your plan's usage limits; if you hit
one, the run stops cleanly and the next run picks up where it left off. `--model sonnet` uses
less of your allowance than the default `opus`.

## Next steps

The prototype runs daily on a Mac and publishes here. Next: let the first weeks of results decide
what to improve (the first review is due 6 November 2026), then start on the public website (phase 2).

