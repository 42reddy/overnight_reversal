"""
execution.py  —  Order placement for the overnight-reversal basket (Kotak Neo)
────────────────────────────────────────────────────────────────────────────
Same three passes a day as before, now routed through Kotak Neo's Trade API
(neo_api_client / NeoAPI) instead of Upstox — Upstox is no longer involved in
trading at all, only in market data (see live_engine.py / auth.py).

  1. ENTRY (~09:15): place_entry_basket() fires the whole sized basket as a
     ladder of IOC (immediate-or-cancel) LIMIT waves — STRATEGY.entry_ladder_bps,
     e.g. 10/25/45bps — instead of one resting DAY-limit order per name.
     Each wave places every still-outstanding name's order IN PARALLEL
     (STRATEGY.entry_parallelism workers), then polls briefly for the
     (near-instant, since IOC) fill result before either moving on or
     retrying the residual at the next, wider rung. Buy limit = ask *
     (1 + cushion), sell limit = bid * (1 - cushion), anchored to the LIVE
     best bid/ask from live_engine.LiveQuoteStreamer at the moment each rung
     fires (falling back to signal_price * (1 ± cushion) for any name the
     stream has no depth for) — both legs on the same intraday product
     (config STRATEGY.long_product/short_product, default MIS, no MTF
     financing on either leg since nothing is held overnight). Because IOC
     either fills (fully/partially) or is cancelled by the exchange
     essentially immediately, the whole ladder resolves in a few seconds
     — there's nothing left resting for the old up-to-5-minute
     market_open→entry_cutoff window to matter for anymore; a name still
     short after the last rung is simply left partially filled (logged),
     the same outcome a cancelled resting order used to produce.

  2. CANCEL UNFILLED (~09:20): defensive backstop only now — IOC orders
     don't rest, so this shouldn't normally find anything open. Kept in
     case a rung's order somehow comes back in a non-terminal state.

  3. EXIT (~15:00): MARKET orders closing out whatever Kotak's live position
     book actually shows open — reconciled against get_net_positions(), NOT
     just replayed off the bot's own entry-side state, so a position closed
     or resized manually outside the bot gets flattened correctly instead of
     reversed into a fresh position.

Orders are addressed by Kotak's `trading_symbol` (NSE cash-equity convention
is "<TICKER>-EQ", e.g. "RELIANCE-EQ") on exchange_segment "nse_cm" — this
bot's own `ticker` keys already match Kotak's underlying NSE symbol, so no
separate instrument-master resolution step is needed for orders the way
Upstox's ISIN-keyed instrument_key required (instrument_master.py still
exists, but only resolves Upstox instrument_key for market-data calls).

Kotak's order-status vocabulary (the `ordSt` field) isn't exhaustively
published (see github.com/Kotak-Neo/kotak-neo-api discussion #263), so
rather than enumerate every "still open" spelling, TERMINAL_STATUSES below
enumerates the statuses known to mean "done" and anything else is treated
as still-open/pending. Confirm live against your own account's order book
before relying on this for the first live day — if Kotak returns a
terminal-status spelling not listed here, add it.

DRY-RUN (config.ini [SANDBOX] enabled=true): Kotak has no public retail
paper-trading environment the way Upstox's sandbox was, so "sandbox" here
means a purely local simulation — no network calls to Kotak at all. Entries
fill instantly (first ladder rung, full qty) at the computed limit price;
exits fill at that same recorded price (so simulated day PnL is always ~0
by construction). It exercises the full mechanics — login, signals, sizing,
state/log bookkeeping, timing — without ever touching a real account. It is
NOT a broker-side fill/slippage test, and it can't exercise the multi-rung
ladder or a genuinely partial fill the way live trading can.

CONCURRENCY: place_entry_basket() places orders for a whole ladder rung in
parallel via a thread pool (STRATEGY.entry_parallelism). This relies on the
installed Kotak SDK (kotakneoapi, httpx-based pooled client + its own
thread-safe rate limiter) tolerating concurrent place_order/order_report
calls from multiple threads on the same client — not independently
live-verified end-to-end here; watch the first few live parallel mornings
against the actual Kotak order book (no duplicate/dropped orders) before
trusting this at higher concurrency. All state.py/trade_log.py bookkeeping
stays on the caller's single thread (see bot.run_entry_pass) — only the
broker network calls are parallelized.
"""

import concurrent.futures
import logging
import time
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Optional

from neo_api_client import NeoAPI

logger = logging.getLogger(__name__)

EXCHANGE_SEGMENT = "nse_cm"

TERMINAL_STATUSES = {
    "complete", "completed", "traded", "rejected", "cancelled", "canceled", "expired",
}


@dataclass
class OrderSnapshot:
    order_id: str
    status: str
    filled_quantity: int
    average_price: Optional[float]


def _is_open(status: str) -> bool:
    return (status or "").strip().lower() not in TERMINAL_STATUSES


def _to_float(val, default=None):
    try:
        if val in (None, "", "-", "NA"):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_int(val, default=0):
    try:
        if val in (None, "", "-", "NA"):
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _trading_symbol(ticker: str) -> str:
    """Kotak Neo cash-equity trading symbols are NSE_SYMBOL + "-EQ"."""
    return f"{ticker}-EQ"


def _ticker_from_trading_symbol(trading_symbol: str) -> str:
    return trading_symbol[:-3] if trading_symbol.endswith("-EQ") else trading_symbol


def _round_to_tick(price: float, tick: float = 0.05) -> float:
    """NSE cash-equity tick size is Rs 0.05 for the overwhelming majority of
    listed equities — snap the computed limit price to a valid tick so the
    exchange doesn't reject the order for an invalid price increment. (A
    handful of very low-priced/illiquid names can carry a different tick
    size; not handled here since instrument_master.py doesn't resolve tick
    size today — if you trade such a name, verify its tick manually.)"""
    return round(round(price / tick) * tick, 2)


class Executor:
    def __init__(self, client: NeoAPI, cfg: ConfigParser):
        self.client = client
        s = cfg["STRATEGY"]
        self.long_product = s.get("long_product", "MIS")
        self.short_product = s.get("short_product", "MIS")
        self.entry_ladder_bps = [float(x) for x in s.get("entry_ladder_bps", "10,25,45").split(",") if x.strip()]
        self.entry_ladder_poll_interval_s = float(s.get("entry_ladder_poll_interval_s", 0.4))
        self.entry_ladder_poll_timeout_s = float(s.get("entry_ladder_poll_timeout_s", 2.5))
        self.entry_parallelism = int(s.get("entry_parallelism", 8))
        self.dry_run = cfg["SANDBOX"].getboolean("enabled", fallback=True)

        # dry-run only: local simulated broker state
        self._sim_orders = {}       # order_id -> OrderSnapshot
        self._sim_positions = {}    # ticker -> signed net qty
        self._sim_entry_price = {}  # ticker -> price the (single) open leg was simulated at

    # ── Entry ────────────────────────────────────────────────────

    def _rung_limit_price(self, direction: str, signal_price: float, cushion_bps: float, touch) -> float:
        """
        touch: (bid, ask) from LiveQuoteStreamer.get_touch(), or None.

        Anchors to the LIVE best bid/ask when known (buy = ask*(1+cushion),
        sell = bid*(1-cushion) — cushion is headroom against the touch
        moving between our quote read and the order landing at the
        exchange, not a spread-crossing margin, since ask/bid are already
        marketable). Falls back to signal_price*(1±cushion) — the old flat
        behaviour — for a name the stream has no depth for.
        """
        buffer_frac = cushion_bps / 10_000.0
        if touch is not None:
            bid, ask = touch
            ref = ask if direction == "long" else bid
            if ref and ref > 0:
                raw = ref * (1 + buffer_frac) if direction == "long" else ref * (1 - buffer_frac)
                return _round_to_tick(raw)
        raw = signal_price * (1 + buffer_frac if direction == "long" else 1 - buffer_frac)
        return _round_to_tick(raw)

    def _place_ioc(self, ticker: str, direction: str, qty: int, limit_price: float, product: str) -> Optional[str]:
        """Fires one IOC LIMIT order. Returns order_id, or None if rejected
        outright (never sent, or Kotak returned an error)."""
        transaction = "B" if direction == "long" else "S"

        if self.dry_run:
            order_id = f"SIM-{ticker}-ENTRY-{int(time.time() * 1000)}"
            self._sim_orders[order_id] = OrderSnapshot(order_id, "complete", qty, limit_price)
            signed = qty if direction == "long" else -qty
            self._sim_positions[ticker] = self._sim_positions.get(ticker, 0) + signed
            self._sim_entry_price[ticker] = limit_price
            logger.info(f"[DRY-RUN] ENTRY simulated: {ticker} {transaction} {qty} "
                        f"(LMT {limit_price}) product={product} order_id={order_id}")
            return order_id

        try:
            resp = self.client.place_order(
                exchange_segment=EXCHANGE_SEGMENT,
                product=product,
                price=str(limit_price),
                order_type="L",
                quantity=str(qty),
                validity="IOC",
                trading_symbol=_trading_symbol(ticker),
                transaction_type=transaction,
                amo="NO",
                disclosed_quantity="0",
                trigger_price="0",
            )
        except Exception as e:
            logger.error(f"IOC ENTRY FAILED: {ticker} {transaction} {qty} @ {limit_price} error={e}")
            return None

        order_id = self._extract_order_id(resp, ticker, transaction, qty, "IOC ENTRY")
        if order_id is not None:
            logger.info(f"IOC ENTRY placed: {ticker} {transaction} {qty} (LMT {limit_price}) "
                        f"product={product} order_id={order_id}")
        return order_id

    def place_entry_basket(self, positions: list, quote_provider=None) -> dict:
        """
        Fires entries for the whole sized basket as parallel IOC waves
        across entry_ladder_bps rungs (see module docstring), instead of
        one resting DAY-limit order per name.

        positions: sized position dicts from sizing.PositionSizer
            ({ticker, instrument_key, direction, qty, price, ...}).
        quote_provider: optional callable(instrument_key) -> (bid, ask) |
            None — normally live_engine.LiveQuoteStreamer.get_touch, read
            fresh at the moment each rung fires (see _rung_limit_price).

        Each wave: submit an IOC order for every name with qty still
        outstanding, in parallel (entry_parallelism workers); then poll the
        order book (entry_ladder_poll_interval_s / _timeout_s) until every
        order from this wave is resolved (IOC settles near-instantly) or
        the wave's timeout elapses. A name still short after the last rung
        is left with whatever it filled.

        Returns {ticker: {"order_ids": [...], "filled_qty": int,
        "avg_fill_price": float | None, "last_limit_price": float | None,
        "status": "filled" | "partial" | "cancelled" | "rejected"}}.
        """
        by_ticker = {p["ticker"]: p for p in positions}
        remaining = {p["ticker"]: p["qty"] for p in positions}
        order_ids = {p["ticker"]: [] for p in positions}
        fills = {p["ticker"]: [] for p in positions}   # list of (filled_qty, avg_price)
        last_limit_price = {}

        for rung_idx, cushion_bps in enumerate(self.entry_ladder_bps, start=1):
            wave = [t for t, q in remaining.items() if q > 0]
            if not wave:
                break
            logger.info(f"Entry ladder rung {rung_idx}/{len(self.entry_ladder_bps)} "
                        f"({cushion_bps:.0f}bps cushion): {len(wave)} name(s) outstanding")

            wave_orders = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.entry_parallelism) as pool:
                futures = {}
                for ticker in wave:
                    pos = by_ticker[ticker]
                    touch = quote_provider(pos["instrument_key"]) if quote_provider else None
                    limit_price = self._rung_limit_price(pos["direction"], pos["price"], cushion_bps, touch)
                    last_limit_price[ticker] = limit_price
                    product = self.long_product if pos["direction"] == "long" else self.short_product
                    fut = pool.submit(self._place_ioc, ticker, pos["direction"],
                                       remaining[ticker], limit_price, product)
                    futures[fut] = ticker
                for fut in concurrent.futures.as_completed(futures):
                    ticker = futures[fut]
                    try:
                        order_id = fut.result()
                    except Exception:
                        logger.exception(f"{ticker}: entry ladder rung {rung_idx} placement crashed")
                        order_id = None
                    if order_id:
                        order_ids[ticker].append(order_id)
                        wave_orders[ticker] = order_id

            if self.dry_run:
                # Sim fills resolve synchronously inside _place_ioc.
                for ticker, order_id in wave_orders.items():
                    snap = self._sim_orders[order_id]
                    fills[ticker].append((snap.filled_quantity, snap.average_price))
                    remaining[ticker] -= snap.filled_quantity
                continue

            pending = dict(wave_orders)
            deadline = time.monotonic() + self.entry_ladder_poll_timeout_s
            while pending and time.monotonic() < deadline:
                book = self.get_order_book()
                for ticker, order_id in list(pending.items()):
                    o = book.get(order_id)
                    if o is None or _is_open(o.status):
                        continue
                    fills[ticker].append((o.filled_quantity, o.average_price))
                    remaining[ticker] -= o.filled_quantity
                    del pending[ticker]
                if pending:
                    time.sleep(self.entry_ladder_poll_interval_s)
            if pending:
                # IOC resolves near-instantly at the exchange; anything still
                # "pending" here after the poll budget is unusual — read back
                # whatever's on file one more time and move on rather than
                # block the rest of the basket/day on it.
                book = self.get_order_book()
                for ticker, order_id in pending.items():
                    o = book.get(order_id)
                    if o is not None:
                        fills[ticker].append((o.filled_quantity or 0, o.average_price))
                        remaining[ticker] -= (o.filled_quantity or 0)
                    else:
                        logger.warning(f"{ticker}: IOC order {order_id} (rung {rung_idx}) not found "
                                        f"in the order book after {self.entry_ladder_poll_timeout_s:.1f}s")

        results = {}
        for ticker, pos in by_ticker.items():
            requested = pos["qty"]
            filled_qty = sum(q for q, _ in fills[ticker])
            priced_notional = sum(q * p for q, p in fills[ticker] if p)
            avg_price = (priced_notional / filled_qty) if filled_qty > 0 else None
            status = self._resolve_ladder_status(filled_qty, requested, order_ids[ticker])
            results[ticker] = {
                "order_ids": order_ids[ticker],
                "filled_qty": filled_qty,
                "avg_fill_price": avg_price,
                "last_limit_price": last_limit_price.get(ticker),
                "status": status,
            }
            logger.info(f"ENTRY {ticker} {pos['direction'].upper()} ladder done: "
                        f"{filled_qty}/{requested} filled @ avg={avg_price} status={status} "
                        f"orders={order_ids[ticker]}")
        return results

    @staticmethod
    def _resolve_ladder_status(filled_qty: int, requested_qty: int, order_ids: list) -> str:
        if filled_qty >= requested_qty and requested_qty > 0:
            return "filled"
        if filled_qty > 0:
            return "partial"
        if not order_ids:
            return "rejected"
        return "cancelled"

    @staticmethod
    def _extract_order_id(resp, ticker, transaction, qty, label) -> Optional[str]:
        """Kotak's place_order returns a dict like {"stat": "Ok", "nOrdNo": "...",
        "stCode": 200} on success. Never let a malformed/unexpected response
        crash the rest of the entry/exit loop for other positions — the order
        may still be LIVE and untracked, so this is logged loud enough to find
        manually in the Kotak Neo order book."""
        if not isinstance(resp, dict):
            logger.error(f"{label} placed at Kotak but response could not be parsed — "
                         f"CHECK THE KOTAK NEO ORDER BOOK MANUALLY for {ticker} "
                         f"{transaction} {qty} (MARKET). Raw response: {resp!r}")
            return None
        if resp.get("Error Message"):
            # The SDK returns this client-side, with no network call at all, whenever
            # configuration.edit_token/edit_sid are unset — i.e. the session never
            # actually completed 2FA. No order was sent to Kotak; nothing to check
            # in the order book. See auth.py get_kotak_client for the real fix.
            logger.error(f"{label} NEVER SENT — Kotak session is not authenticated "
                         f"(2FA incomplete): {ticker} {transaction} {qty} response={resp}")
            return None
        if str(resp.get("stat", "")).lower() not in ("ok", ""):
            logger.error(f"{label} FAILED: {ticker} {transaction} {qty} response={resp}")
            return None
        order_id = resp.get("nOrdNo")
        if not order_id:
            logger.error(f"{label} placed at Kotak but no nOrdNo in response — "
                         f"CHECK THE KOTAK NEO ORDER BOOK MANUALLY for {ticker} "
                         f"{transaction} {qty} (MARKET). Raw response: {resp!r}")
            return None
        return str(order_id)

    # ── Order-book polling / cancellation ───────────────────────

    def get_order_book(self) -> dict:
        """{order_id: OrderSnapshot} for every order Kotak Neo has on file today."""
        if self.dry_run:
            return dict(self._sim_orders)
        try:
            resp = self.client.order_report()
        except Exception as e:
            logger.error(f"order_report failed: {e}")
            return {}
        if not isinstance(resp, dict) or str(resp.get("stat", "")).lower() not in ("ok", ""):
            logger.error(f"order_report returned an error: {resp}")
            return {}

        book = {}
        for o in (resp.get("data") or []):
            order_id = o.get("nOrdNo")
            if not order_id:
                continue
            book[str(order_id)] = OrderSnapshot(
                order_id=str(order_id),
                status=str(o.get("ordSt", "")),
                filled_quantity=_to_int(o.get("fldQty"), 0),
                average_price=_to_float(o.get("avgPrc")),
            )
        return book

    def cancel_unfilled(self, order_ids: dict) -> dict:
        """
        order_ids: {ticker: order_id}. Cancels anything still open, leaves
        filled orders alone. Returns {ticker: OrderSnapshot} with the final
        snapshot for every order (so callers can read fill qty/price).
        """
        book = self.get_order_book()
        final = {}
        for ticker, order_id in order_ids.items():
            if order_id is None:
                continue
            o = book.get(order_id)
            if o is None:
                logger.warning(f"{ticker}: order {order_id} not found in order book")
                continue
            final[ticker] = o
            if _is_open(o.status):
                if self.dry_run:
                    continue
                try:
                    self.client.cancel_order(order_id=order_id)
                    logger.info(f"CANCELLED unfilled entry: {ticker} order_id={order_id} "
                                f"(was {o.status}, filled_qty={o.filled_quantity})")
                except Exception as e:
                    logger.error(f"Cancel FAILED: {ticker} order_id={order_id} error={e}")
        return final

    # ── Exit ─────────────────────────────────────────────────────

    def get_net_positions(self) -> dict:
        """
        {ticker: net_qty} from Kotak's live position book for today.
        Positive = net long, negative = net short; a ticker with no live
        position simply won't be a key (treat missing as 0).

        This is the source of truth for the exit pass — see place_exit.
        """
        if self.dry_run:
            return dict(self._sim_positions)

        try:
            resp = self.client.positions()
        except Exception as e:
            logger.error(f"positions() failed: {e}")
            return {}
        if not isinstance(resp, dict) or str(resp.get("stat", "")).lower() not in ("ok", ""):
            logger.error(f"positions() returned an error: {resp}")
            return {}

        net = {}
        for p in (resp.get("data") or []):
            trading_symbol = p.get("trdSym") or p.get("sym", "")
            ticker = _ticker_from_trading_symbol(trading_symbol)
            if not ticker:
                continue
            # Carry-forward (cf*) qty should always be 0 for this bot — every
            # position is opened and closed same day — but included for
            # correctness in case something was left open from outside it.
            buy_qty = _to_int(p.get("flBuyQty")) + _to_int(p.get("cfBuyQty"))
            sell_qty = _to_int(p.get("flSellQty")) + _to_int(p.get("cfSellQty"))
            net[ticker] = net.get(ticker, 0) + (buy_qty - sell_qty)
        return net

    def place_exit(self, ticker: str, instrument_key: str, qty: int, transaction: str) -> str:
        """
        Market-order flatten of `qty` shares of `ticker`.
        `transaction` is "SELL" (closing a net-long position) or "BUY"
        (closing a net-short position) — the caller determines both from
        the *live* net position (get_net_positions), not from the bot's own
        stale idea of which direction it originally opened, so a position
        that was closed or resized manually outside the bot gets flattened
        correctly instead of reversed into a fresh position.
        Returns order_id or None. `instrument_key` is accepted for call-site
        symmetry with the sizing/state schema but unused here — Kotak orders
        route by trading_symbol, not the Upstox-format instrument_key.
        """
        product = self.long_product if transaction == "SELL" else self.short_product
        transaction_code = "S" if transaction == "SELL" else "B"

        if self.dry_run:
            order_id = f"SIM-{ticker}-EXIT-{int(time.time() * 1000)}"
            price = self._sim_entry_price.get(ticker, 0.0)
            self._sim_orders[order_id] = OrderSnapshot(order_id, "complete", qty, price)
            self._sim_positions[ticker] = 0
            logger.info(f"[DRY-RUN] EXIT simulated: {ticker} {transaction} {qty} (MKT) "
                        f"order_id={order_id}")
            return order_id

        try:
            resp = self.client.place_order(
                exchange_segment=EXCHANGE_SEGMENT,
                product=product,
                price="0",
                order_type="MKT",
                quantity=str(qty),
                validity="DAY",
                trading_symbol=_trading_symbol(ticker),
                transaction_type=transaction_code,
                amo="NO",
                disclosed_quantity="0",
                trigger_price="0",
            )
        except Exception as e:
            logger.error(f"EXIT FAILED: {ticker} {transaction} {qty} error={e}")
            return None

        order_id = self._extract_order_id(resp, ticker, transaction, qty, "EXIT")
        if order_id is None:
            return None

        logger.info(f"EXIT placed: {ticker} {transaction} {qty} (MKT) order_id={order_id}")
        return order_id

    def place_exits(self, flatten_orders: list) -> dict:
        """
        flatten_orders: list of dicts {ticker, instrument_key, exit_qty,
        exit_transaction} — already reconciled against the live position
        book by the caller (see bot.run_exit_pass). Returns {ticker: order_id}.
        """
        results = {}
        for pos in flatten_orders:
            results[pos["ticker"]] = self.place_exit(
                pos["ticker"], pos.get("instrument_key"), pos["exit_qty"], pos["exit_transaction"]
            )
        return results

    def confirm_fills(self, order_ids: dict, timeout_s: float = 20.0, poll_s: float = 2.0) -> dict:
        """
        Poll the order book until every order_id is out of an open status or
        `timeout_s` elapses. Returns {ticker: OrderSnapshot} final snapshot.
        Used after both entries (post-cancel) and exits, to read back actual
        fill qty/price for the trade log.
        """
        pending = {t: oid for t, oid in order_ids.items() if oid is not None}
        final = {}
        deadline = time.monotonic() + timeout_s
        while pending and time.monotonic() < deadline:
            book = self.get_order_book()
            still_pending = {}
            for ticker, oid in pending.items():
                o = book.get(oid)
                if o is None:
                    still_pending[ticker] = oid
                    continue
                if _is_open(o.status):
                    still_pending[ticker] = oid
                else:
                    final[ticker] = o
            pending = still_pending
            if pending:
                time.sleep(poll_s)

        if pending:
            logger.warning(f"confirm_fills: {list(pending)} still unresolved after "
                           f"{timeout_s}s — last known status will be used")
            book = self.get_order_book()
            for ticker, oid in pending.items():
                o = book.get(oid)
                if o is not None:
                    final[ticker] = o
        return final
