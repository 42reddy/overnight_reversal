"""
execution.py  —  Order placement for the overnight-reversal basket (Kotak Neo)
────────────────────────────────────────────────────────────────────────────
Same three passes a day as before, now routed through Kotak Neo's Trade API
(neo_api_client / NeoAPI) instead of Upstox — Upstox is no longer involved in
trading at all, only in market data (see live_engine.py / auth.py).

  1. ENTRY (~09:15): one MARKET order per sized position, both legs on the
     same intraday product (config STRATEGY.long_product/short_product,
     default MIS — cash-segment intraday, no MTF financing on either leg
     since nothing is held overnight).

  2. CANCEL UNFILLED (~09:20): safety net for any entry that comes back
     open/pending — MARKET orders normally resolve immediately.

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
fill instantly at the signal price; exits fill at that same recorded price
(so simulated day PnL is always ~0 by construction). It exercises the full
mechanics — login, signals, sizing, state/log bookkeeping, timing — without
ever touching a real account. It is NOT a broker-side fill/slippage test.
"""

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


class Executor:
    def __init__(self, client: NeoAPI, cfg: ConfigParser):
        self.client = client
        s = cfg["STRATEGY"]
        self.long_product = s.get("long_product", "MIS")
        self.short_product = s.get("short_product", "MIS")
        self.dry_run = cfg["SANDBOX"].getboolean("enabled", fallback=True)

        # dry-run only: local simulated broker state
        self._sim_orders = {}       # order_id -> OrderSnapshot
        self._sim_positions = {}    # ticker -> signed net qty
        self._sim_entry_price = {}  # ticker -> price the (single) open leg was simulated at

    # ── Entry ────────────────────────────────────────────────────

    def place_entry(self, position: dict) -> tuple:
        """
        Place one MARKET entry order for a sized position dict (from
        sizing.PositionSizer): {ticker, instrument_key, direction, qty, price}.

        Returns (order_id, signal_price) — order_id is None if the order
        was rejected outright.
        """
        ticker = position["ticker"]
        direction = position["direction"]
        transaction = "B" if direction == "long" else "S"
        product = self.long_product if direction == "long" else self.short_product
        signal_price = position["price"]
        qty = position["qty"]

        if self.dry_run:
            order_id = f"SIM-{ticker}-ENTRY-{int(time.time() * 1000)}"
            self._sim_orders[order_id] = OrderSnapshot(order_id, "complete", qty, signal_price)
            signed = qty if direction == "long" else -qty
            self._sim_positions[ticker] = self._sim_positions.get(ticker, 0) + signed
            self._sim_entry_price[ticker] = signal_price
            logger.info(f"[DRY-RUN] ENTRY simulated: {ticker} {transaction} {qty} (MKT) "
                        f"product={product} order_id={order_id}")
            return order_id, signal_price

        try:
            resp = self.client.place_order(
                exchange_segment=EXCHANGE_SEGMENT,
                product=product,
                price="0",
                order_type="MKT",
                quantity=str(qty),
                validity="DAY",
                trading_symbol=_trading_symbol(ticker),
                transaction_type=transaction,
                amo="NO",
                disclosed_quantity="0",
                trigger_price="0",
            )
        except Exception as e:
            logger.error(f"ENTRY FAILED: {ticker} {transaction} {qty} error={e}")
            return None, signal_price

        order_id = self._extract_order_id(resp, ticker, transaction, qty, "ENTRY")
        if order_id is None:
            return None, signal_price

        logger.info(f"ENTRY placed: {ticker} {transaction} {qty} (MKT) "
                    f"product={product} order_id={order_id}")
        return order_id, signal_price

    def place_entries(self, positions: list) -> dict:
        """Fire one entry order per position, back-to-back. Returns
        {ticker: (order_id, signal_price)}."""
        results = {}
        for pos in positions:
            results[pos["ticker"]] = self.place_entry(pos)
        return results

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
