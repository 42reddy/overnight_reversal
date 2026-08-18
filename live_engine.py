"""
live_engine.py  —  Live cross-sectional overnight-reversal signal engine
──────────────────────────────────────────────────────────────────────────
Same signal minute_backtest.py validates, run live:

  r_co(ticker) = ltp_at_open / prior_session_close - 1
  demeaned      = r_co - mean(r_co across the universe, that morning)
                  — DISABLED for now (see build_signals): ranking uses raw
                  r_co directly, not demeaned against the tape.

  Long  the n_long names with the most NEGATIVE (demeaned) r_co (biggest
        overnight losers relative to the tape — bet on reversal up).
  Short the n_short names with the most POSITIVE (demeaned) r_co (biggest
        overnight winners — bet on reversal down).

Two-step, matched to the trading day:
  1. fetch_prev_closes() — call once after login (well before 09:15); each
     name's prior completed session close doesn't change intraday, so this
     is cached for the rest of the day.
  2. build_signals()     — call at/just after 09:15; ONE bulk LTP call for
     the whole universe (not one call per name), so ranking + order
     placement happens in a couple of seconds, not a couple of minutes.
"""

import logging

import upstox_client
from upstox_client.rest import ApiException

from sizing import load_instruments

logger = logging.getLogger(__name__)

API_VERSION = "2.0"


class SignalEngine:
    def __init__(self, cfg, api_client: upstox_client.ApiClient, instruments: dict = None):
        s = cfg["STRATEGY"]
        self.n_long = int(s["n_long"])
        self.n_short = int(s["n_short"])
        self.max_share_price = float(s["max_share_price"])
        self.max_overnight_move_pct = float(s["max_overnight_move_pct"])
        self.instruments = instruments if instruments is not None else load_instruments(cfg)

        self.history_api = upstox_client.HistoryApi(api_client)
        self.quote_api = upstox_client.MarketQuoteApi(api_client)

        self.prev_close = {}   # ticker -> float, filled by fetch_prev_closes()

    def _tradeable_tickers(self):
        return [t for t, info in self.instruments.items()
                if not info.get("exclude") and info.get("instrument_key")]

    # ── Step 1: prior close (once per day, cacheable) ──────────────

    def fetch_prev_closes(self) -> dict:
        """
        Populate self.prev_close for every tradeable ticker with the last
        completed session's close. Names that fail to resolve are dropped
        from today's universe (logged, not fatal to the whole run).
        """
        tickers = self._tradeable_tickers()
        import datetime as dt
        to_date = dt.date.today().isoformat()

        prev_close = {}
        failed = []
        for ticker in tickers:
            key = self.instruments[ticker]["instrument_key"]
            try:
                resp = self.history_api.get_historical_candle_data(
                    instrument_key=key, interval="day", to_date=to_date,
                    api_version=API_VERSION,
                )
                candles = resp.data.candles if resp.data else []
                if not candles:
                    failed.append(ticker)
                    continue
                candles = sorted(candles, key=lambda c: c[0], reverse=True)
                prev_close[ticker] = float(candles[0][4])  # [ts,o,h,l,close,vol,oi]
            except ApiException as e:
                logger.warning(f"{ticker}: prev-close fetch failed "
                                f"(status={e.status}) — dropping for today")
                failed.append(ticker)
            except Exception as e:
                logger.warning(f"{ticker}: prev-close fetch error ({e}) — dropping for today")
                failed.append(ticker)

        self.prev_close = prev_close
        logger.info(f"Prior-close resolved for {len(prev_close)}/{len(tickers)} tickers"
                    + (f"; failed: {failed}" if failed else ""))
        return prev_close

    # ── Step 2: open-price ranking (call at/after 09:15) ────────────

    def _fetch_ltp_bulk(self, tickers) -> dict:
        """One bulk LTP call for every ticker with a resolved prev_close."""
        keys = [self.instruments[t]["instrument_key"] for t in tickers]
        key_to_ticker = {self.instruments[t]["instrument_key"]: t for t in tickers}
        if not keys:
            return {}

        ltp = {}
        try:
            resp = self.quote_api.ltp(symbol=",".join(keys), api_version=API_VERSION)
            for entry in (resp.data or {}).values():
                ticker = key_to_ticker.get(entry.instrument_token)
                if ticker:
                    ltp[ticker] = float(entry.last_price)
        except ApiException as e:
            logger.error(f"Bulk LTP fetch failed: status={e.status} body={e.body}")
        return ltp

    def build_signals(self) -> list:
        """
        Ranks the universe on this morning's overnight return and returns
        the full pool of tradeable candidates, sorted ascending by
        overnight_ret (most negative — biggest losers — first):
            [{"ticker", "instrument_key", "price", "overnight_ret"}, ...]

        This is the whole surviving pool, NOT just the n_long + n_short
        basket — sizing.PositionSizer.size_positions() picks the actual
        long/short legs from it (walking inward from both ends of the
        sort), backfilling from the next-ranked candidate whenever one
        can't be sized (no instrument_key, qty rounds to 0, etc.) so a
        sizing-stage drop doesn't just shrink the basket.
        """
        tickers = [t for t in self._tradeable_tickers() if t in self.prev_close]
        if not tickers:
            logger.error("No tickers with a resolved prior close — call fetch_prev_closes() first")
            return []

        ltp = self._fetch_ltp_bulk(tickers)

        rows = []
        for ticker in tickers:
            price = ltp.get(ticker)
            prev = self.prev_close.get(ticker)
            if price is None or price <= 0 or prev is None or prev <= 0:
                continue
            if price > self.max_share_price:
                continue
            r_co = price / prev - 1.0
            if abs(r_co) >= self.max_overnight_move_pct / 100.0:
                continue
            rows.append({"ticker": ticker, "instrument_key": self.instruments[ticker]["instrument_key"],
                         "price": price, "prev_close": prev, "r_co": r_co})

        if len(rows) < self.n_long + self.n_short:
            logger.warning(f"Only {len(rows)} names have usable open-price data today "
                           f"(need {self.n_long + self.n_short} for a full basket)")

        if not rows:
            return []

        #mean_r = sum(r["r_co"] for r in rows) / len(rows)
        for r in rows:
            r["overnight_ret"] = r["r_co"]  # demeaning disabled for now

        rows.sort(key=lambda r: r["overnight_ret"])  # ascending: most negative first

        candidates = [{"ticker": r["ticker"], "instrument_key": r["instrument_key"],
                       "price": r["price"], "overnight_ret": r["overnight_ret"]} for r in rows]

        logger.info(
            f"Ranked {len(candidates)} candidate(s): most-negative "
            + ", ".join(f"{c['ticker']}({c['overnight_ret']:+.2%})" for c in candidates[:5])
            + "  ...  most-positive "
            + ", ".join(f"{c['ticker']}({c['overnight_ret']:+.2%})" for c in reversed(candidates[-5:]))
        )
        return candidates
