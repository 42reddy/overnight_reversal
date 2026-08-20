# Overnight-Reversal Basket Bot — Kotak Neo (trading) + Upstox (data)

Cross-sectional overnight-reversal basket: every morning, rank a curated
NSE universe by overnight return (this morning's LTP vs. prior session
close), long the biggest losers, short the biggest winners, hold intraday,
exit before the close. See `PIPELINE.md` for the full file-by-file data flow.

Orders, cancels, and position reads all go through **Kotak Neo**. Market
data (prior close, LTP) comes from **Upstox**'s long-lived Analytics Access
Token, kept because Upstox's data feed has proven more reliable — Upstox is
never involved in placing an order.

## File structure

```
overnight_reversal/
├── config.ini          ← strategy params & timing (NEVER commit real secrets)
├── requirements.txt
├── auth.py              ← Kotak Neo TOTP+MPIN login + Upstox analytics-token client
├── execution.py          ← Kotak Neo order placement/cancel/positions
├── live_engine.py        ← Upstox market-data signal engine (SignalEngine)
├── sizing.py              ← PositionSizer — capital → sized orders
├── state.py                ← persistent basket/position tracker
├── trade_log.py             ← per-day trade journal + portfolio PnL
├── instrument_master.py      ← resolves Upstox instrument_key for market data
├── instruments.json            ← the ticker universe
├── bot.py                       ← headless scheduler loop (main entry point)
├── app.py                        ← Streamlit UI over the same ReversalBot
├── logs/                          ← auto-created
└── state/                          ← auto-created
```

## Setup

```bash
pip install -r requirements.txt
pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
```

## One-time Kotak Neo setup

1. In the Kotak Neo app → More → Trade API → Create New Application. This
   issues a single token (labelled "API Access Token" / "Consumer Key" on
   screen — current Neo onboarding gives just this one token, not a
   separate key+secret pair) → `KOTAK_ACCESS_TOKEN` in `.env`.
2. From the same Trade API page, register for TOTP if you haven't already.
   Save the base32 secret shown → `KOTAK_TOTP_SECRET` in `.env` (NOT the
   6-digit code — the long string).
3. Set `KOTAK_MOBILE_NUMBER` (registered mobile, e.g. `+919999999999`),
   `KOTAK_UCC` (your Kotak client code), and `KOTAK_MPIN` (trading MPIN)
   in `.env`.
4. `python bot.py` logs in fresh every trading day — there's no daily
   manual step once `.env` is set. If a login ever fails complaining about
   the token/consumer key specifically (not TOTP/MPIN), regenerate
   `KOTAK_ACCESS_TOKEN` from the app — it may carry its own expiry.

## One-time Upstox setup (market data only)

1. Go to https://developer.upstox.com → generate a long-lived **Analytics
   Access Token** (1-year validity, read-only Market Data + Streaming
   scope) from the Developer Console.
2. Set `UPSTOX_ANALYTICS_TOKEN` in `.env`. No daily login needed for this —
   it's used only for prior-close/LTP pulls, never for orders.

## `.env` summary

```
KOTAK_ACCESS_TOKEN=
KOTAK_MOBILE_NUMBER=
KOTAK_UCC=
KOTAK_MPIN=
KOTAK_TOTP_SECRET=
UPSTOX_ANALYTICS_TOKEN=
```

Never commit `.env` — it should be gitignored.

## Dry-run — USE THIS FIRST

In config.ini:
```ini
[SANDBOX]
enabled = true
```

## Universe / instrument tokens

`instruments.json` is the curated ticker list. `instrument_master.py`
auto-fills each ticker's Upstox `instrument_key` (used only for market-data
calls) — run it standalone or let `bot.py` do it at startup. Kotak orders
route by `trading_symbol` (`<TICKER>-EQ`), derived directly from the ticker
in `instruments.json` — no separate resolution step needed for orders.

## Running

```bash
python bot.py
```

Persistent mode (default, no args) sleeps through nights/weekends/holidays
and runs one trading day at a time forever. For a single day and exit:

```bash
python bot.py --once
```

For persistent deployment on a VPS:
```bash
nohup python bot.py >> logs/bot.log 2>&1 &
```

Add to crontab for auto-start on reboot:
```
@reboot sleep 30 && cd ~/overnight_reversal && source ~/venv/bin/activate && nohup python bot.py >> logs/bot.log 2>&1 &
```

Or drive it interactively via the Streamlit UI:
```bash
streamlit run app.py
```

## Before going live

- Confirm Kotak Neo's MIS auto-square-off cutoff/penalty policy for your
  account and check it against `config.ini`'s `[TIMING] exit_start` /
  `exit_deadline` — these were tuned against Upstox's confirmed-live
  behaviour and have not been independently re-verified for Kotak.
- Place one small manual test order through the SDK and check the exact
  `ordSt` status string Kotak returns at each stage (open/filled/rejected/
  cancelled) against `execution.py`'s `TERMINAL_STATUSES` — Kotak's status
  vocabulary isn't exhaustively documented publicly.
