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

Open http://127.0.0.1:8000. To customise, copy `config.example.yaml` to `config.yaml`, edit it, and
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
  or half the debit. Exit if a short strike's delta reaches 0.45. Close by 14:45 on expiry day.
- **Entry gates:** entries only between 09:30 and 14:30 IST. No entries on event days, on stale data,
  within 30 minutes of the last entry on that symbol, or after a 1.2% move inside 15 minutes.
- **Realistic paper fills:** fills cross part of the bid-ask spread. Charges cover brokerage, STT,
  exchange fees, SEBI fees, stamp duty and GST.

All of these thresholds are in `config.example.yaml`.

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
- For real-time data, a broker API (Zerodha Kite, Upstox, Angel One SmartAPI) can be added as another
  provider implementing `optionsbot/data/base.py`.

## Layout

```
optionsbot/
  analytics/   greeks.py (Black-Scholes, IV), chain_metrics.py (GEX, OI, skew), indicators.py
  data/        nse.py (live), yahoo.py (history), simulated.py (synthetic market)
  strategies.py  regime detection, strike search, EV scoring
  risk.py        sizing, limits, circuit breaker, entry gates
  paper.py       paper broker: fills, mark-to-market, exits
  bot.py         orchestration loop
  server.py      FastAPI + WebSocket; web/ holds the dashboard
```
