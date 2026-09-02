"""
market_cap.py  —  Refresh instruments.json's market_cap field (offline, periodic)
──────────────────────────────────────────────────────────────────────────────
live_engine.py weights its cross-sectional demeaning mean by sqrt(market_cap)
instead of treating every name equally — a name like a large-cap pulls "the
tape" much harder than a microcap, so it should count for more when
estimating the common overnight move to strip out. Nothing in the live
trading path (Upstox market data, Kotak orders) exposes market cap, so this
is fetched separately, offline, via yfinance (already a project dependency
for the backtests) and cached as a plain field on each ticker in
instruments.json.

Market cap moves slowly relative to day-to-day overnight-return dispersion,
so this does NOT need to run every trading day — unlike
instrument_master.py's instrument_key resolution, bot.py does not call this
automatically at startup. Re-run it by hand every few weeks/months:

    python market_cap.py

Or call resolve_market_caps() programmatically. Tickers that fail to
resolve keep whatever market_cap value (if any) was already on file rather
than being blanked out — a transient Yahoo Finance hiccup on one name
shouldn't erase a still-reasonably-current cached value.
"""

import json
import logging
import os
import time

import yfinance as yf

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INSTRUMENTS_FILE = os.path.join(HERE, "instruments.json")

# Small delay between sequential Yahoo Finance calls — this is a one-shot
# maintenance script, not on the trading day's critical path, so there's no
# reason to hammer the endpoint and risk a rate-limit block for the ~339
# names in the current universe.
REQUEST_DELAY_S = 0.3


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _yf_symbol(ticker: str) -> str:
    """NSE tickers -> yfinance convention, same as the backtests' TICKERS list."""
    return f"{ticker}.NS"


def _fetch_one_market_cap(ticker: str):
    """
    Returns a positive float market cap (in rupees) or None if it couldn't
    be resolved. Tries fast_info first (cheap, one lightweight call);
    falls back to the heavier .info dict if fast_info doesn't have it —
    both are yfinance's own documented ways to get market cap, kept as a
    fallback pair because either one can intermittently omit the field for
    a given ticker without erroring.
    """
    symbol = _yf_symbol(ticker)
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        mcap = fi.get("marketCap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
        if mcap and mcap > 0:
            return float(mcap)
        info = t.info
        mcap = info.get("marketCap")
        if mcap and mcap > 0:
            return float(mcap)
    except Exception as e:
        logger.warning(f"{ticker}: market cap fetch failed ({e})")
        return None
    return None


def resolve_market_caps(instruments_file=DEFAULT_INSTRUMENTS_FILE) -> tuple:
    """
    Fill in / refresh market_cap for every non-excluded ticker in
    instruments.json. Writes the file back in place.

    Returns (resolved: list[str], failed: list[str]) ticker symbols.
    """
    data = _load_json(instruments_file)
    universe = data["universe"]

    resolved, failed = [], []
    for i, (ticker, info) in enumerate(universe.items()):
        if info.get("exclude"):
            continue
        mcap = _fetch_one_market_cap(ticker)
        if mcap is None:
            failed.append(ticker)
        else:
            info["market_cap"] = mcap
            resolved.append(ticker)
        time.sleep(REQUEST_DELAY_S)
        if (i + 1) % 50 == 0:
            logger.info(f"...{i + 1}/{len(universe)} processed")

    _save_json(instruments_file, data)

    if failed:
        logger.warning(
            f"{len(failed)} ticker(s) could not resolve a market cap this run "
            f"(kept whatever value, if any, was already on file): {failed}"
        )
    logger.info(f"Refreshed market_cap for {len(resolved)}/{len(universe)} tickers")
    return resolved, failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    resolve_market_caps()
