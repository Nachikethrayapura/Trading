# NSE Options Bot

A live-updating options **recommendation and paper-trading** bot for NSE F&O (NIFTY, BANKNIFTY,
FINNIFTY, MIDCPNIFTY and stock options). It reads the option chain the way index-options desks do,
recommends **defined-risk** trades only, sizes them to a fixed risk budget, and paper-trades them
with hard loss limits, all on a browser dashboard that refreshes live.

> ⚠️ Educational tool, not investment advice. It never places real orders. Paper results include
> modelled slippage and charges, but they do not guarantee live results. Run it on paper for several weeks
> and review the closed-trade log before trusting any signal with money.

## Quick start

```bash
pip install -r requirements.txt

# Live NSE data (run on your own machine; NSE blocks most cloud/datacenter IPs)
python -m optionsbot

# Synthetic market: works any time (nights, weekends) and plays a trading day out in minutes
python -m optionsbot --simulate
```

Open http://127.0.0.1:8000. For real-time data through your broker, see
[Broker data](#broker-data-real-time). To customise, copy `config.example.yaml` to `config.yaml`, edit it, and
run `python -m optionsbot --config config.yaml`. Paper trades persist in `data/paper_trades.db`
(the simulator uses a separate `data/paper_trades_sim.db`, so synthetic trades never mix with live ones).

Run tests with `python -m pytest`.

## How it decides

Every poll (default 60s) and for each symbol, the bot works through these steps:

1. **Reads the chain** (NSE public API) and price history (Yahoo), then picks the nearest expiry 2–35 days out.
2. **Computes institutional chain analytics**
   - **Volatility risk premium (VRP):** ATM implied vol vs 20-day realised vol, and IV rank against the
     1-year India VIX range. Index desks systematically sell vol when implied is rich.
   - **Dealer gamma exposure (GEX)** and the **gamma-flip level.** Positive GEX means dealers hedge
     against moves, so price tends to pin and range. Negative GEX means hedging amplifies moves, so price trends.
   - **OI positioning:** put/call walls (heaviest writing = support/resistance), PCR, max pain, and
     fresh put-vs-call writing near the money.
   - **25-delta skew**, plus the expected move (±1σ to expiry).
3. **Reads the trend** with EMA 9/21, VWAP, Supertrend and RSI on 5-minute bars, plus the daily 20-EMA.
   This is blended with OI flow into a direction score from −1 to +1.
4. **Chooses a regime:**

   | Condition | Trade |
   |---|---|
   | Vol rich, no trend, positive gamma | Iron condor |
   | Vol rich, bullish / bearish | Bull put / bear call credit spread |
   | Vol not rich, strong trend | Bull call / bear put debit spread |
   | Anything else (incl. rich vol with negative gamma and no trend) | **No trade** |

5. **Searches strikes and scores them.** Every candidate structure is scored by **expected value after
   all charges**, under a *physical* distribution (a blend of realised and implied vol, with a 10%
   fat-tail regime at 2× vol). It is not scored under the risk-neutral one, so a trade only qualifies
   when it has an edge. Candidates also need a minimum confidence, liquid strikes (OI and bid-ask
   filters) and credit-to-width limits.

## How it limits losses

- **Defined risk only:** every short option is covered by a long one; naked selling is impossible.
- **Position sizing:** a trade's *maximum* loss plus costs is capped at 1% of capital. A trade that
  can't fit even one lot is never suggested.
- **Portfolio limits:** at most 5% of capital at risk across all positions, 3 open positions, and 1 per symbol.
- **Circuit breaker:** at −2% on the day (realised + unrealised), the bot flattens everything and
  halts until tomorrow. Three consecutive losers also halt new entries.
- **Exits:** take profit at 50% of max profit (credit) or 60% (debit). Stop at a loss equal to the credit,
  or half the debit. Close by 14:45 on expiry day. An optional delta exit (`short_delta_exit`) is off by
  default because it lost money in backtests.
- **What it trades by default:** only iron condors with expiries at least 7 days out. These were the only
  structures that held up on unseen data (see Backtesting). Enable the others in `strategy.enabled_strategies`.
- **Exchange holidays:** the NSE provider asks NSE whether the market is open, so holidays block entries.
- **Entry gates:** entries only between 09:30 and 14:30 IST. No entries on event days, on stale data,
  within 30 minutes of the last entry on that symbol, or after a 1.2% move inside 15 minutes.
- **Realistic paper fills:** fills cross part of the bid-ask spread. Charges cover brokerage, STT,
  exchange fees, SEBI fees, stamp duty and GST.

All of these thresholds are in `config.example.yaml`.

## Backtesting

```bash
python -m optionsbot.backtest --symbols NIFTY BANKNIFTY --years 3      # downloads from Yahoo
python -m optionsbot.backtest --csv-dir history/                       # offline: NIFTY.csv, INDIAVIX.csv …
```

The backtest replays daily index prices through the *same* engine, risk manager and paper broker.
It prints return, CAGR, max drawdown, win rate and profit factor, broken down by strategy and exit
reason. It also writes `trades.csv`, `equity.csv` and `summary.json` to `reports/backtest/`.

Free historical NSE option-chain data doesn't exist, so each day's chain is **reconstructed** from
the index price and the previous day's India VIX close. VIX is the market's 30-day NIFTY implied
vol; other indices use a fixed multiple of it. The replay uses the same smile and spread model as
the simulator. Keep these limits in mind:

- OI signals (dealer gamma, walls, PCR) have no history, so they are neutral in backtests.
- Each day is four ticks: open, the two extremes, close. Entries happen only at the open
  (any later tick would leak the day's outcome), and stops and targets are checked along that path.
- Real skew, liquidity and fills differ from the model.

### Results on real history (Oct 2023 – Oct 2026, NIFTY + BANKNIFTY, ₹5 lakh)

Settings were chosen on the first half and then checked on the second half, which they never saw:

| Settings | Oct 2023 – Apr 2025 (tuning) | Apr 2025 – Oct 2026 (unseen) |
|---|---|---|
| Original: all five spreads, 2+ day expiries, 0.45-delta exit | −6.5%, max DD 7.7%, PF 0.44 | −5.2%, max DD 5.9%, PF 0.52 |
| **Current defaults:** iron condors only, 7+ day expiries, no delta exit | +2.2%, max DD 1.1%, PF 4.7 | +0.35%, max DD 1.0%, PF 1.5 |

Over the full three years the defaults made +2.6% (max drawdown 1.1%, 14 trades). The directional
trend spreads lost money in every variant tried, which is why they are off by default. The trade
counts are small and the edge is close to zero: the defaults mostly reduce losses rather than
generate returns. Thirteen variants were compared on the tuning half.

Treat a backtest as a check on the rules and risk controls, not a forecast. Use it to switch off
strategy families that lose money (`strategy.enabled_strategies`) before you paper-trade live.

## Broker data (real-time)

NSE's public website data is free but delayed and fragile. If you have a broker API subscription,
use it for real-time chains:

| Broker | One-time setup | Each trading day | Run |
|---|---|---|---|
| Upstox | Create an app at the Upstox developer console. Set `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI` | `python -m optionsbot.auth upstox` | `python -m optionsbot --provider upstox` |
| Zerodha Kite | Create a Kite Connect app. Set `KITE_API_KEY`, `KITE_API_SECRET` | `python -m optionsbot.auth kite` | `python -m optionsbot --provider kite` |

The login helper prints a URL. Log in, then paste the URL you're redirected to. The access token is
saved to `data/broker_tokens.json` (file mode 600) and expires the next morning. Lot sizes are read
from the broker's contract master, so they stay current.

Things to know:

- Upstox supplies IV; for Kite, IV is backed out from the mid price.
- Kite has no previous-day OI, so OI change is measured from the first reading of the session.
- Brokers usually report OI in shares, while the analytics expect contracts. `data.broker_oi_unit`
  controls the conversion. Check one strike against nseindia.com.
- Trend history still comes from Yahoo.
- The bot only **reads** data. It never places orders.

## Telegram alerts

1. In Telegram, message **@BotFather** and send `/newbot`. Copy the token, then `export TELEGRAM_BOT_TOKEN=...`
2. Send any message to your new bot, then run `python -m optionsbot.notify --find-chat` and export
   the `TELEGRAM_CHAT_ID` it prints.
3. Run `python -m optionsbot.notify "hello"` to send a test.

From then on the bot sends alerts for:

- every paper entry and exit, with P&L
- circuit-breaker trips
- data outages (at most hourly)
- new actionable recommendations, when auto paper-trading is off (at most every 30 minutes per idea)

## Shareable replay

Record a live session with `python -m optionsbot --record session.jsonl`, then build a page from it
(plus a backtest) with `python -m optionsbot.demo --recording session.jsonl --backtest reports/backtest`.
`python -m optionsbot.demo --days 3 --out demo.html` instead records a few simulated trading days and builds
a single self-contained HTML file that replays them in the real dashboard. It needs no server, so you
can share it. All data in it is synthetic; live market data needs the bot running on your machine.

## Dashboard

- Account equity, day P&L against the loss limit, open risk, track record and circuit-breaker state.
- Per-symbol analytics, the current regime and an interactive OI-by-strike chart.
- Recommendations with legs, credit or debit, max profit and loss per lot, probability of profit, EV,
  confidence, size, exit levels and the reasons behind each.
- Open positions with live P&L and a close button; equity curve; closed-trade log; activity feed.
- Controls: pause/resume, auto paper-trade on/off (off = recommend only, with a button to paper-trade
  a recommendation), and flatten all.

## Data caveats

- NSE's website API is unofficial and undocumented. It needs browser-like headers and cookies,
  rate-limits aggressively, and sometimes changes shape. The client supports both the legacy
  `option-chain-indices` endpoint and the newer `option-chain-contract-info` + `option-chain-v3` pair.
  It never silently falls back to fake data: a failing feed shows as an error banner and blocks new entries.
- Yahoo intraday bars can lag by a few minutes.
- Check the lot sizes, expiry rules and STT rates in the config against current NSE/SEBI circulars.
  These change often.
- For real-time data use a broker provider (above). Other brokers can be added by implementing
  `optionsbot/data/base.py`.

## Layout

```
optionsbot/
  analytics/   greeks.py (Black-Scholes, IV), chain_metrics.py (GEX, OI, skew), indicators.py
  data/        nse.py (public), brokers.py (Upstox, Kite), yahoo.py (history),
               simulated.py + synthetic.py (synthetic market / chain reconstruction)
  backtest.py    historical replay through the live engine
  notify.py      Telegram alerts;  auth.py  daily broker login
  strategies.py  regime detection, strike search, EV scoring
  risk.py        sizing, limits, circuit breaker, entry gates
  paper.py       paper broker: fills, mark-to-market, exits
  bot.py         orchestration loop
  server.py      FastAPI + WebSocket; web/ holds the dashboard
```
