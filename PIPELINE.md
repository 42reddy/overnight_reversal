# Pipeline

How the overnight-reversal basket bot fits together, file by file.

## Strategy in one paragraph

Every morning: rank a curated universe by overnight return (this morning's
LTP vs. prior session close), demean it cross-sectionally to strip out the
common market move, long the biggest losers, short the biggest winners.
Hold intraday, exit before the close. Capital is split into `n_splits`
equal slots (one per name), each MTF-leveraged per-ticker.

## Files

| File | Role |
|---|---|
| `config.ini` | All tunables: credentials, strategy params, timing, file paths. Read once at startup. |
| `instruments.json` | **The ticker universe.** `{ticker: {instrument_key, exclude, mtf_leverage}}`. Edit this to add/remove/exclude names. |
| `auth.py` | Logs into Upstox. Sandbox: reads a pre-issued `sandbox_token`. Live: TOTP login using mobile/password/pin/totp_secret. Caches the day's access token to `state/token.json`. |
| `instrument_master.py` | Downloads Upstox's NSE instrument master and fills in `instrument_key` for any ticker in `instruments.json` missing one. Run standalone (`python instrument_master.py`) or it runs automatically once at bot startup. |
| `live_engine.py` (`SignalEngine`) | The signal. `fetch_prev_closes()` (once, before the open) gets each ticker's last session close. `build_signals()` (at 09:15) does one bulk LTP call, computes demeaned overnight return, and picks the long/short basket. |
| `sizing.py` (`PositionSizer`) | Turns signals into sized orders: capital / n_splits per slot, notional = slot × leverage, qty = floor(notional / price). Drops a name if excluded, over `max_share_price`, or too small for 1 share. |
| `execution.py` (`Executor`) | Places orders via Upstox `OrderApiV3`. Entry = LIMIT, offset from signal price by `entry_limit_buffer_bps`. Cancel-unfilled at 09:20. Exit = MARKET at 15:20, longs on `MTF`, shorts on `I` (intraday cash short). |
| `state.py` (`BasketState`) | Persists today's basket (`state/position.json`) — ticker, direction, qty, order ids, fill status. Lets the bot (or the UI) restart mid-day without losing track of open legs. |
| `trade_log.py` (`TradeLogger`) | Append-only day-by-day journal (`logs/trade_log.json`) with per-leg PnL and running portfolio totals (win rate, drawdown, etc). This is what the Streamlit calendar/portfolio tabs read. |
| `bot.py` (`ReversalBot`) | Orchestrates one trading day: login → refresh instrument keys → fetch prior closes → wait for open → entry pass → wait → cancel-unfilled → wait for exit window → exit pass → finalize day. Headless entry point (`python bot.py`). |
| `app.py` | Streamlit UI wrapping the same `ReversalBot` — buttons instead of a wait loop, plus Today/Calendar/Portfolio tabs. Reads/writes the same `state.py` / `trade_log.py` files as `bot.py`, so either front-end can pick up where the other left off. |

### Research / offline only (not part of the live path)

| File | Role |
|---|---|
| `overnight_reversal.py` | Sanity check on daily OHLC: is overnight return actually negatively correlated with same-day intraday return (reversal, not momentum)? |
| `minute_backtest.py` | Backtest using real 1-min bars with retail-realistic entry/exit timing, instead of the noisy open/close auction prints. |
| `metrics.py` | Generic equity-curve / performance-stat helpers (CAGR, drawdown, etc.) used by the backtests above. |

## Live data flow, in order

```
config.ini ──┐
             ├─▶ bot.py: load_config()
instruments.json (universe) ──▶ sizing.load_instruments() ──┬─▶ SignalEngine
                                                              └─▶ PositionSizer

1. auth.py.get_client()            → api_client (sandbox or live)
2. instrument_master.py            → fills missing instrument_key values in instruments.json
3. SignalEngine.fetch_prev_closes()→ prior close per ticker (slow, done before 09:15)
4. [wait until market_open]
5. SignalEngine.build_signals()    → one bulk LTP call, ranked demeaned overnight-return basket
6. PositionSizer.size_positions()  → qty per name (capital/n_splits × leverage, floor by price)
7. Executor.place_entry() × N      → LIMIT orders  ┐
   state.add_planned_position()                    ├─ written to state/position.json
   trade_log.log_entry_order()                      └─ and logs/trade_log.json
8. [wait until entry_cutoff]
9. Executor.cancel_unfilled()      → cancels anything still open
10. [wait until exit_start]
11. Executor.place_exits()         → MARKET orders closing every filled leg
12. Executor.confirm_fills()       → reads back actual fill price/qty
13. trade_log.finalize_day()       → computes day PnL, rolls into portfolio totals
```

`app.py` drives the same 7 numbered steps via buttons instead of `wait_until()`
sleeps — you click "1. Fetch prior closes" any time before the open, "2. Run
entry" at/after 09:15, "3. Cancel unfilled" around 09:20, "4. Run exit" around
15:20. The Calendar and Portfolio tabs are pure reads of `logs/trade_log.json`.

## Where to change things

- **Universe** → `instruments.json`
- **How many longs/shorts, capital, leverage, price cap** → `config.ini` `[STRATEGY]`
- **Entry/exit timing** → `config.ini` `[TIMING]`
- **Sandbox vs. live** → `config.ini` `[SANDBOX] enabled`
