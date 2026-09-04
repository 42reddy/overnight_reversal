"""
live_engine.py  —  Live cross-sectional overnight-reversal signal engine
──────────────────────────────────────────────────────────────────────────
Same signal minute_backtest.py validates, run live:

  r_co(ticker) = ltp_at_open / prior_session_close - 1
  demeaned      = r_co - weighted_mean(r_co across the universe, that
                  morning), weight = sqrt(market_cap) — ranking uses the
                  demeaned value. This is a deliberate DIVERGENCE from
                  daily_data_backtest.py's demean_cross_sectionally(),
                  which uses a plain equal-weighted mean: a name like a
                  large-cap actually moves/represents "the tape" much more
                  than a microcap does, so it should pull the estimated
                  common move harder. sqrt() (rather than raw market cap)
                  keeps the single largest name in the universe from
                  dominating the mean outright. See market_cap.py for
                  where market_cap comes from (instruments.json, refreshed
                  offline/periodically — it isn't available from any live
                  market-data call this bot makes).

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
import time

import upstox_client
from upstox_client.rest import ApiException

from sizing import load_instruments

logger = logging.getLogger(__name__)

API_VERSION = "2.0"

# The underlying upstox_client REST layer defaults to NO request timeout
# (waits forever) unless one is passed explicitly. fetch_prev_closes() makes
# one HTTP call per ticker, sequentially — without this, a single stalled
# connection anywhere in ~300+ calls hangs the entire prep pass indefinitely,
# with no error and no way to tell it apart from just being slow.
REQUEST_TIMEOUT_S = 15


class SignalEngine:
    def __init__(self, cfg, api_client: upstox_client.ApiClient, instruments: dict = None):
        s = cfg["STRATEGY"]
        self.n_long = int(s["n_long"])
        self.n_short = int(s["n_short"])
        self.max_share_price = float(s["max_share_price"])
        self.max_overnight_move_pct = float(s["max_overnight_move_pct"])
        self.max_data_error_pct = float(s.get("max_data_error_pct", 25.0))
        self.instruments = instruments if instruments is not None else load_instruments(cfg)

        self.history_api = upstox_client.HistoryApi(api_client)
        self.quote_api = upstox_client.MarketQuoteApi(api_client)

        self.prev_close = {}   # ticker -> float, filled by fetch_prev_closes()

    def _tradeable_tickers(self):
        return [t for t, info in self.instruments.items()
                if not info.get("exclude") and info.get("instrument_key")]

    # ── Step 1: prior close (once per day, cacheable) ──────────────

    def fetch_prev_closes(self, max_seconds: float = None) -> dict:
        """
        Populate self.prev_close for every tradeable ticker with the last
        completed session's close. Names that fail to resolve are dropped
        from today's universe (logged, not fatal to the whole run).

        max_seconds: hard wall-clock budget for the whole loop (not per
        ticker — REQUEST_TIMEOUT_S already bounds that). This is a
        sequential, one-call-per-ticker loop over ~300+ names, run during
        the narrow pre-market prep window (see bot.py's TIMING.prep_start /
        market_open) — a run of slow/timed-out tickers on a rough network
        morning could otherwise eat into or past market_open, which cuts
        directly into (or eliminates) the entry window. Once the budget is
        spent, remaining tickers are dropped for today exactly like an
        individual fetch failure (loud warning, not fatal) rather than
        risking the whole day's entries over prior-close data for names
        not yet reached. bot.py passes the actual seconds remaining until
        market_open; left None (e.g. for standalone/manual runs) this is
        unbounded, same as before.
        """
        tickers = self._tradeable_tickers()
        import datetime as dt
        to_date = dt.date.today().isoformat()

        logger.info(f"Fetching previous close prices for {len(tickers)} ticker(s)...")
        prev_close = {}
        failed = []
        start = time.monotonic()
        for i, ticker in enumerate(tickers):
            if max_seconds is not None and (time.monotonic() - start) > max_seconds:
                skipped = tickers[i:]
                logger.error(
                    f"fetch_prev_closes: {max_seconds:.0f}s prep budget exhausted with "
                    f"{len(skipped)}/{len(tickers)} ticker(s) not yet attempted — dropping "
                    f"them for today rather than risk running past market open: {skipped[:10]}"
                    + (" ..." if len(skipped) > 10 else "")
                )
                failed.extend(skipped)
                break
            key = self.instruments[ticker]["instrument_key"]
            try:
                resp = self.history_api.get_historical_candle_data(
                    instrument_key=key, interval="day", to_date=to_date,
                    api_version=API_VERSION, _request_timeout=REQUEST_TIMEOUT_S,
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
        logger.info(f"Fetched previous close prices for {len(prev_close)}/{len(tickers)} ticker(s)"
                    + (f"; failed: {failed}" if failed else ""))
        return prev_close

    # ── Step 2: open-price ranking (call at/after 09:15) ────────────

    def _fetch_ltp_bulk(self, tickers) -> dict:
        """One bulk LTP call for every ticker with a resolved prev_close."""
        keys = [self.instruments[t]["instrument_key"] for t in tickers]
        key_to_ticker = {self.instruments[t]["instrument_key"]: t for t in tickers}
        if not keys:
            return {}

        logger.info(f"Fetching open prices (LTP) for {len(keys)} ticker(s)...")
        ltp = {}
        try:
            resp = self.quote_api.ltp(symbol=",".join(keys), api_version=API_VERSION,
                                       _request_timeout=REQUEST_TIMEOUT_S)
            for entry in (resp.data or {}).values():
                ticker = key_to_ticker.get(entry.instrument_token)
                if ticker:
                    ltp[ticker] = float(entry.last_price)
            logger.info(f"Fetched open prices for {len(ltp)}/{len(keys)} ticker(s)")
        except ApiException as e:
            logger.error(f"Bulk LTP fetch failed: status={e.status} body={e.body}")
        except Exception as e:
            # A timed-out connection surfaces as a raw urllib3/socket exception,
            # not an ApiException — without this, it would propagate uncaught
            # out of build_signals() and crash the entry pass instead of just
            # skipping today's entries gracefully (see run_entry_pass's
            # "no signals available" branch).
            logger.error(f"Bulk LTP fetch error: {e}")
        return ltp

    def build_signals(self) -> list:
        """
        Ranks the universe on this morning's overnight return and returns
        the full pool of tradeable candidates, sorted ascending by
        overnight_ret (most negative — biggest losers, cross-sectionally
        demeaned — first):
            [{"ticker", "instrument_key", "price", "overnight_ret", "raw_overnight_ret"}, ...]

        This is the whole surviving pool, NOT just the n_long + n_short
        basket — sizing.PositionSizer.size_positions() picks the actual
        long/short legs from it (walking inward from both ends of the
        sort), backfilling from the next-ranked candidate whenever one
        can't be sized (no instrument_key, qty rounds to 0, etc.) so a
        sizing-stage drop doesn't just shrink the basket.

        The order-of-operations (mean computed before the momentum filter
        is applied — momentum names are excluded from the ranking pool but
        still count towards estimating "the tape") matches
        daily_data_backtest.py's build_positions(). The mean ITSELF does
        not: it's a sqrt(market_cap)-weighted mean here, not the backtest's
        plain equal-weighted one — see the module docstring for why. A
        ticker missing market_cap (see market_cap.py) is excluded from the
        mean estimate but still ranked against it.
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
            if abs(r_co) >= self.max_data_error_pct / 100.0:
                logger.warning(f"{ticker}: overnight move {r_co:+.2%} looks like a data error "
                               f"(prior close or LTP is likely stale/wrong, or an unadjusted "
                               f"corporate action) — dropping for today, not just for ranking")
                continue
            mcap = self.instruments[ticker].get("market_cap")
            weight = mcap ** 0.5 if mcap and mcap > 0 else None
            rows.append({"ticker": ticker, "instrument_key": self.instruments[ticker]["instrument_key"],
                         "price": price, "prev_close": prev, "r_co": r_co, "_weight": weight})

        if not rows:
            return []

        weighted = [r for r in rows if r["_weight"] is not None]
        unweighted = [r for r in rows if r["_weight"] is None]
        if unweighted:
            logger.warning(
                f"{len(unweighted)}/{len(rows)} ticker(s) have no market_cap on file "
                f"(run market_cap.py) — excluded from the market-mean estimate, still "
                f"ranked against it: {[r['ticker'] for r in unweighted][:10]}"
                + (" ..." if len(unweighted) > 10 else "")
            )

        if weighted:
            total_weight = sum(r["_weight"] for r in weighted)
            mean_r = sum(r["_weight"] * r["r_co"] for r in weighted) / total_weight
        else:
            logger.warning("No ticker has a market_cap on file — falling back to an "
                           "equal-weighted mean for today (run market_cap.py)")
            mean_r = sum(r["r_co"] for r in rows) / len(rows)

        for r in rows:
            r["overnight_ret"] = r["r_co"] - mean_r
            del r["_weight"]

        candidates_pool = [r for r in rows if abs(r["r_co"]) <= self.max_overnight_move_pct / 100.0]

        if len(candidates_pool) < self.n_long + self.n_short:
            logger.warning(f"Only {len(candidates_pool)} names pass the momentum filter today "
                           f"(need {self.n_long + self.n_short} for a full basket)")

        if not candidates_pool:
            return []

        candidates_pool.sort(key=lambda r: r["overnight_ret"])  # ascending: most negative (demeaned) first

        candidates = [{"ticker": r["ticker"], "instrument_key": r["instrument_key"],
                       "price": r["price"], "overnight_ret": r["overnight_ret"],
                       "raw_overnight_ret": r["r_co"]} for r in candidates_pool]

        logger.info(
            f"Ranked {len(candidates)} candidate(s) vs. {mean_r:+.2%} market mean "
            f"(n={len(rows)}): most-negative "
            + ", ".join(f"{c['ticker']}({c['overnight_ret']:+.2%})" for c in candidates[:5])
            + "  ...  most-positive "
            + ", ".join(f"{c['ticker']}({c['overnight_ret']:+.2%})" for c in reversed(candidates[-5:]))
        )
        return candidates
