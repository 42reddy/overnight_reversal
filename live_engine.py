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

Three-step, matched to the trading day:
  1. fetch_prev_closes() — call once after login (well before 09:15); each
     name's prior completed session close doesn't change intraday, so this
     is cached for the rest of the day.
  2. start_streaming()   — call once prior closes are in (still well before
     09:15), opens a WebSocket subscription to the whole tradeable universe
     via LiveQuoteStreamer. Ticks (and top-of-book bid/ask) are already
     flowing into an in-memory cache by the time the open happens, so
     there's no request-response round trip sitting between "market opens"
     and "we know the price" the way a REST call fired after market_open
     would have — see build_signals()/_capture_open_quotes().
  3. build_signals()     — call at/just after 09:15; reads the streamed
     open-price cache (falling back to a one-off REST LTP pull for any
     name that hasn't ticked yet), ranks the universe, and returns the
     candidate pool.
"""

import logging
import threading
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


class LiveQuoteStreamer:
    """
    Wraps Upstox's WebSocket market-data feed (MarketDataStreamerV3, "full"
    mode) with an in-memory {instrument_key: {"ltp", "bid", "ask", "ts"}}
    cache, continuously updated by the SDK's own background WS thread.

    Two jobs:
      - build_signals()'s open-price capture reads off this cache instead
        of firing a bulk REST LTP call after market_open unblocks (see
        SignalEngine._capture_open_quotes) — the connection is opened
        during the pre-market prep window, well before 09:15, so it's
        already receiving ticks by the time the open happens.
      - execution.Executor.place_entry_basket() reads get_touch() live, at
        the moment each entry-ladder rung fires, to anchor that rung's
        limit price to the current best bid/ask instead of a flat bps off
        the (by-then possibly stale) signal price.

    Thread-safety: ticks arrive on the SDK's own background WS thread;
    every read/write of the cache goes through `_lock`.
    """

    def __init__(self, api_client: upstox_client.ApiClient, instrument_keys: list):
        self._streamer = upstox_client.MarketDataStreamerV3(
            api_client, instrumentKeys=list(instrument_keys), mode="full")
        self._lock = threading.Lock()
        self._quotes = {}  # instrument_key -> {"ltp": float, "bid": float, "ask": float, "ts": float}
        self._opened = threading.Event()
        self._streamer.on("open", lambda: self._opened.set())
        self._streamer.on("message", self._on_message)
        self._streamer.on("error", self._on_error)

    def start(self, connect_timeout_s: float = 10.0):
        self._streamer.connect()
        if not self._opened.wait(connect_timeout_s):
            logger.warning(
                f"Upstox market-data stream did not confirm open within "
                f"{connect_timeout_s:.0f}s — continuing anyway; build_signals() "
                f"will fall back to REST LTP for whatever hasn't streamed a tick "
                f"by open_capture_window_s"
            )
        else:
            logger.info(f"Market-data stream connected, subscribed to "
                        f"{len(self._streamer.instrumentKeys)} instrument(s)")

    def stop(self):
        try:
            self._streamer.disconnect()
        except Exception as e:
            logger.warning(f"Market-data stream disconnect error (harmless if already closed): {e}")

    def _on_error(self, err):
        logger.warning(f"Market-data stream error: {err}")

    def _on_message(self, data: dict):
        ts = time.monotonic()
        feeds = (data or {}).get("feeds") or {}
        if not feeds:
            return
        with self._lock:
            for key, feed in feeds.items():
                full = ((feed.get("fullFeed") or {}).get("marketFF")) or {}
                ltpc = full.get("ltpc") or {}
                ltp = ltpc.get("ltp")
                levels = ((full.get("marketLevel") or {}).get("bidAskQuote")) or []
                bid = levels[0].get("bidP") if levels else None
                ask = levels[0].get("askP") if levels else None
                if ltp is None and bid is None and ask is None:
                    continue
                entry = self._quotes.setdefault(key, {})
                if ltp is not None:
                    entry["ltp"] = float(ltp)
                if bid is not None:
                    entry["bid"] = float(bid)
                if ask is not None:
                    entry["ask"] = float(ask)
                entry["ts"] = ts

    def snapshot_ltp(self, instrument_keys) -> dict:
        """{instrument_key: ltp} for every key that's ticked so far."""
        with self._lock:
            return {k: self._quotes[k]["ltp"] for k in instrument_keys
                    if k in self._quotes and "ltp" in self._quotes[k]}

    def get_touch(self, instrument_key):
        """(bid, ask) from the latest tick, or None if not (yet) known —
        used by execution.py to anchor an entry-ladder rung's limit price."""
        with self._lock:
            q = self._quotes.get(instrument_key)
            if not q or "bid" not in q or "ask" not in q:
                return None
            return q["bid"], q["ask"]

    def wait_for_keys(self, instrument_keys, deadline_monotonic: float) -> list:
        """Blocks (short sleeps) until every key has a tick or the deadline
        passes. Returns whichever keys are still missing."""
        while True:
            with self._lock:
                missing = [k for k in instrument_keys if k not in self._quotes]
            remaining = deadline_monotonic - time.monotonic()
            if not missing or remaining <= 0:
                return missing
            time.sleep(min(0.2, remaining))


class SignalEngine:
    def __init__(self, cfg, api_client: upstox_client.ApiClient, instruments: dict = None):
        s = cfg["STRATEGY"]
        self.n_long = int(s["n_long"])
        self.n_short = int(s["n_short"])
        self.max_share_price = float(s["max_share_price"])
        self.max_overnight_move_pct = float(s["max_overnight_move_pct"])
        self.max_data_error_pct = float(s.get("max_data_error_pct", 25.0))
        self.instruments = instruments if instruments is not None else load_instruments(cfg)

        self._api_client = api_client
        self.history_api = upstox_client.HistoryApi(api_client)
        self.quote_api = upstox_client.MarketQuoteApi(api_client)

        self.open_capture_window_s = float(cfg["TIMING"].get("open_capture_window_s", 4.0))

        self.prev_close = {}   # ticker -> float, filled by fetch_prev_closes()
        self.streamer: LiveQuoteStreamer = None   # set by start_streaming()

    def _tradeable_tickers(self):
        return [t for t, info in self.instruments.items()
                if not info.get("exclude") and info.get("instrument_key")]

    # ── Step 2: open the market-data stream (call once prior closes are in,
    #    still well before 09:15) ───────────────────────────────────────

    def start_streaming(self):
        """
        Opens the WebSocket subscription for today's tradeable universe
        (every ticker with a resolved prev_close — call after
        fetch_prev_closes()) so ticks are already flowing into
        LiveQuoteStreamer's in-memory cache by the time market_open hits,
        instead of build_signals() firing a bulk REST LTP call and waiting
        on that round trip after the open unblocks. Safe to call even if
        the connection is slow/fails — build_signals() falls back to REST
        for whatever hasn't streamed a tick by open_capture_window_s.
        """
        tickers = [t for t in self._tradeable_tickers() if t in self.prev_close]
        keys = [self.instruments[t]["instrument_key"] for t in tickers]
        if not keys:
            logger.warning("start_streaming: no tradeable ticker has a resolved prev_close yet — "
                            "nothing to subscribe (call fetch_prev_closes() first)")
            return
        self.streamer = LiveQuoteStreamer(self._api_client, keys)
        self.streamer.start()

    def stop_streaming(self):
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer = None

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

    # ── Step 3: open-price ranking (call at/after 09:15) ────────────

    def _capture_open_quotes(self, tickers) -> dict:
        """
        Preferred open-price source: read whatever's already accumulated in
        the streaming cache (see start_streaming/LiveQuoteStreamer), waiting
        up to open_capture_window_s total for stragglers that haven't
        printed a first trade yet. Anything still missing after that falls
        back to a one-off bulk REST LTP pull — same call this used to make
        for the *whole* universe unconditionally, now only hit for the
        handful of names the stream didn't cover in time (or the stream
        never came up at all, e.g. a bad connect — see start_streaming).
        """
        if self.streamer is None:
            logger.warning("No market-data stream active — falling back to a bulk REST "
                            "LTP pull for the whole universe (call start_streaming() first "
                            "to avoid this)")
            return self._fetch_ltp_bulk(tickers)

        keys = [self.instruments[t]["instrument_key"] for t in tickers]
        key_to_ticker = {self.instruments[t]["instrument_key"]: t for t in tickers}

        deadline = time.monotonic() + self.open_capture_window_s
        missing_keys = self.streamer.wait_for_keys(keys, deadline)
        ltp = {key_to_ticker[k]: px for k, px in self.streamer.snapshot_ltp(keys).items()}

        if missing_keys:
            missing_tickers = [key_to_ticker[k] for k in missing_keys]
            logger.warning(
                f"{len(missing_tickers)}/{len(tickers)} ticker(s) hadn't streamed a tick "
                f"after {self.open_capture_window_s:.1f}s — falling back to a REST LTP pull "
                f"for just these: {missing_tickers[:10]}" + (" ..." if len(missing_tickers) > 10 else "")
            )
            ltp.update(self._fetch_ltp_bulk(missing_tickers))

        logger.info(f"Captured open prices for {len(ltp)}/{len(tickers)} ticker(s) "
                    f"({len(tickers) - len(missing_keys)} via stream, {len(missing_keys)} via REST fallback)")
        return ltp

    def _fetch_ltp_bulk(self, tickers) -> dict:
        """One bulk LTP call — used as a fallback (see _capture_open_quotes)
        for whatever the market-data stream didn't cover in time, or for the
        whole universe if the stream isn't up at all."""
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

        ltp = self._capture_open_quotes(tickers)

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
