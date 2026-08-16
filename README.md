# NHPC / POWERGRID Stat-Arb Bot — Upstox Edition

## File structure

```
stat_arb/
├── config.ini          ← credentials & strategy params (NEVER commit to git)
├── requirements.txt
├── auth.py             ← Upstox headless TOTP login
├── data.py             ← yfinance spot data (UNCHANGED from Zerodha version)
├── state.py            ← persistent position tracker (UNCHANGED)
├── execution.py        ← Upstox v3 NFO order placement
├── features.py         ← YOUR signal class — copy here
├── bot.py              ← main scheduler loop
├── logs/               ← auto-created
└── state/              ← auto-created
```

## Setup

```bash
pip install -r requirements.txt
```

Copy your `features.py` into this folder.

## One-time Upstox setup (5 minutes)

1. Go to https://developer.upstox.com → My Apps → Create New App
2. Set redirect URL to exactly: `http://127.0.0.1`
3. Copy API Key → `client_id` in config.ini
4. Copy API Secret → `client_secret` in config.ini
5. Enable TOTP in your Upstox account (Settings → Security → TOTP)
6. Copy the raw base32 secret shown during TOTP setup → `totp_secret`
   (NOT the 6-digit code — the long string like `JBSWY3DPEHPK3PXP`)

## Sandbox (paper trading) — USE THIS FIRST

In config.ini:
```ini
[SANDBOX]
enabled = true
```

This routes all orders to Upstox's official sandbox environment.
Real authentication, real signals, zero real money. Run for 2–3 weeks.
Flip to `false` only when you're confident the bot behaves correctly.

## Finding instrument tokens

Upstox uses instrument tokens instead of symbol strings.
Download the NSE instruments file:

```bash
curl -o NSE.json.gz https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
gunzip NSE.json.gz
```

Then find your tokens:
```python
import json
with open("NSE.json") as f:
    instruments = json.load(f)

# Filter: instrument_type = "FUT", name = "NHPC" or "POWERGRID"
# Pick the current month's expiry contract
for inst in instruments:
    if inst.get("name") in ("NHPC", "POWERGRID") and inst.get("instrument_type") == "FUT":
        print(inst["instrument_key"], inst["name"], inst["expiry"])
```

Update `fut_long_token` and `fut_short_token` in config.ini monthly.

## Monthly roll

Update the instrument tokens in config.ini 5 days before the last Thursday
of the month. Format: `NSE_FO|<token>`. Tokens change every month.

## Running

```bash
python bot.py
```

For persistent deployment on a VPS:
```bash
nohup python bot.py >> logs/bot.log 2>&1 &
```

Add to crontab for auto-start on reboot:
```
@reboot sleep 30 && cd ~/stat_arb && source ~/venv/bin/activate && nohup python bot.py >> logs/bot.log 2>&1 &
```

## Cost summary

| Item | Cost |
|---|---|
| Upstox API | Free |
| DigitalOcean 1GB Bangalore Droplet | ~₹500/month |
| **Total infrastructure** | **~₹500/month** |
