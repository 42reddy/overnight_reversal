"""
instrument_master.py  —  Resolve Upstox instrument_key values for the curated universe
────────────────────────────────────────────────────────────────────────────────────
Upstox order/quote/history APIs address instruments by `instrument_key`
(e.g. "NSE_EQ|INE002A01018"), not by trading symbol. That key is only knowable
from Upstox's own instrument master dump — hand-typing ISINs is exactly how
you end up trading the wrong ticker — so this downloads the official NSE
instrument master, matches it against instruments.json's curated symbol
list, and fills in instrument_key for every match.

Run standalone to refresh:
    python instrument_master.py

Or call resolve_instrument_keys() programmatically (bot.py does this once at
startup, before market open, so today's run always has current tokens).

The raw master dump is cached to disk and only re-downloaded once per day —
it's a multi-MB file and doesn't change intraday.
"""

import gzip
import json
import logging
import os
from datetime import date

import requests

logger = logging.getLogger(__name__)

NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INSTRUMENTS_FILE = os.path.join(HERE, "instruments.json")
DEFAULT_CACHE_FILE = os.path.join(HERE, "state", "nse_master_cache.json")

# Equity cash-segment instrument_type values seen in Upstox's dump for plain
# listed shares (as opposed to futures/options contracts on the same name).
EQ_INSTRUMENT_TYPES = {"EQ", "EQUITY"}


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def download_nse_master(cache_file=DEFAULT_CACHE_FILE, force=False):
    """
    Download + cache the NSE instrument master. Re-uses today's cache unless
    force=True. Returns the list of instrument dicts.
    """
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    today = date.today().isoformat()

    if not force and os.path.exists(cache_file):
        try:
            cached = _load_json(cache_file)
            if cached.get("date") == today and cached.get("instruments"):
                logger.info(f"Using cached NSE instrument master from {today} "
                            f"({len(cached['instruments'])} instruments)")
                return cached["instruments"]
        except Exception as e:
            logger.warning(f"Instrument master cache unreadable, re-downloading: {e}")

    logger.info(f"Downloading NSE instrument master from {NSE_MASTER_URL} ...")
    resp = requests.get(NSE_MASTER_URL, timeout=60)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    instruments = json.loads(raw)
    logger.info(f"Downloaded {len(instruments)} NSE instruments")

    _save_json(cache_file, {"date": today, "instruments": instruments})
    return instruments


def _index_by_symbol(master):
    """
    {trading_symbol -> instrument dict} for cash-equity instruments only
    (drop futures/options contracts, which share the same underlying name).
    """
    idx = {}
    for inst in master:
        itype = str(inst.get("instrument_type", "")).upper()
        segment = str(inst.get("segment", "")).upper()
        if itype not in EQ_INSTRUMENT_TYPES and segment != "NSE_EQ":
            continue
        symbol = inst.get("trading_symbol") or inst.get("tradingsymbol")
        if not symbol:
            continue
        idx[symbol.upper()] = inst
    return idx


def resolve_instrument_keys(instruments_file=DEFAULT_INSTRUMENTS_FILE,
                             cache_file=DEFAULT_CACHE_FILE, force_download=False):
    """
    Fill in instrument_key for every non-excluded ticker in instruments.json
    that's missing one. Writes the file back in place.

    Returns (resolved: list[str], missing: list[str]) ticker symbols.
    """
    data = _load_json(instruments_file)
    universe = data["universe"]

    master = download_nse_master(cache_file, force=force_download)
    by_symbol = _index_by_symbol(master)

    resolved, missing = [], []
    for ticker, info in universe.items():
        if info.get("exclude"):
            continue
        inst = by_symbol.get(ticker.upper())
        if inst is None:
            missing.append(ticker)
            continue
        key = inst.get("instrument_key")
        if not key:
            missing.append(ticker)
            continue
        if info.get("instrument_key") != key:
            info["instrument_key"] = key
            info["isin"] = inst.get("isin")
        resolved.append(ticker)

    _save_json(instruments_file, data)

    if missing:
        logger.warning(
            f"{len(missing)} ticker(s) not found in the NSE instrument master "
            f"(check spelling / possibly delisted / renamed): {missing}"
        )
    logger.info(f"Resolved instrument_key for {len(resolved)}/{len(universe)} tickers")
    return resolved, missing


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    resolve_instrument_keys(force_download=True)
