"""
execution.py  —  Order placement for the overnight-reversal basket
──────────────────────────────────────────────────────────────────
Two passes a day:

  1. ENTRY (~09:15): one LIMIT order per sized position, offset from the
     signal price by STRATEGY.entry_limit_buffer_bps *through* the market
     (higher for buys, lower for sells). Default is 0 bps — the LIMIT price
     is placed at exactly the 09:15 open price fetched by build_signals(),
     on the assumption that it's very likely to fill within the 09:15-09:20
     window without needing to chase it through the market; set this > 0 if
     fills start coming back too thin. Both legs go in on product=I
     (cash-segment intraday) at the same flat leverage — nothing is held
     overnight, so there's no reason to pay for MTF financing on the long
     leg, and using one product/leverage on both sides keeps the basket
     dollar-neutral.

  2. CANCEL UNFILLED (~09:20): anything still open/pending gets cancelled —
     we don't chase fills past the first few minutes.

  3. EXIT (~15:20): MARKET orders closing out whatever actually got filled.
     Market orders on liquid large/mid-caps fill within the same second, so
     "exit by 15:20-15:25" is satisfied by firing them at 15:20 and
     confirming fills, not by a second escalation pass.

Uses OrderApiV3 for place/cancel (the current order API) and the v2 OrderApi
for get_order_book, since order-status polling isn't exposed on v3.
"""

import logging
import time
from configparser import ConfigParser

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger(__name__)

API_VERSION = "2.0"


class Executor:
    def __init__(self, api_client: upstox_client.ApiClient, cfg: ConfigParser):
        self.order_v3 = upstox_client.OrderApiV3(api_client)
        self.order_v2 = upstox_client.OrderApi(api_client)
        s = cfg["STRATEGY"]
        self.long_product = s.get("long_product", "MTF")
        self.short_product = s.get("short_product", "I")
        self.buffer_bps = float(s.get("entry_limit_buffer_bps", 15))

    # ── Pricing ──────────────────────────────────────────────────

    def _limit_price(self, price: float, transaction_type: str) -> float:
        """Offset the signal price through the market so the limit order has
        a realistic shot at filling, without chasing an unlimited amount."""
        buf = self.buffer_bps / 10_000.0
        if transaction_type == "BUY":
            px = price * (1 + buf)
        else:
            px = price * (1 - buf)
        return round(px, 1)

    # ── Entry ────────────────────────────────────────────────────

    def place_entry(self, position: dict) -> tuple:
        """
        Place one LIMIT entry order for a sized position dict (from
        sizing.PositionSizer): {ticker, instrument_key, direction, qty, price}.

        Returns (order_id, limit_price) — order_id is None if the order
        was rejected outright.
        """
        ticker = position["ticker"]
        direction = position["direction"]
        transaction = "BUY" if direction == "long" else "SELL"
        product = self.long_product if direction == "long" else self.short_product
        limit_price = self._limit_price(position["price"], transaction)

        body = upstox_client.PlaceOrderV3Request(
            quantity=position["qty"],
            product=product,
            validity="DAY",
            price=limit_price,
            instrument_token=position["instrument_key"],
            order_type="LIMIT",
            transaction_type=transaction,
            disclosed_quantity=0,
            trigger_price=0,
            is_amo=False,
            tag="overnight_reversal",
        )
        try:
            resp = self.order_v3.place_order(body)
            order_id = str(resp.data.order_id)
            logger.info(
                f"ENTRY placed: {ticker} {transaction} {position['qty']}@{limit_price} "
                f"product={product} order_id={order_id}"
            )
            return order_id, limit_price
        except ApiException as e:
            logger.error(f"ENTRY FAILED: {ticker} {transaction} {position['qty']} "
                         f"status={e.status} body={e.body}")
            return None, limit_price

    def place_entries(self, positions: list) -> dict:
        """Fire one entry order per position, back-to-back. Returns
        {ticker: (order_id, limit_price)}."""
        results = {}
        for pos in positions:
            results[pos["ticker"]] = self.place_entry(pos)
        return results

    # ── Order-book polling / cancellation ───────────────────────

    def get_order_book(self) -> dict:
        """{order_id: OrderData} for every order Upstox has on file today."""
        try:
            resp = self.order_v2.get_order_book(api_version=API_VERSION)
            return {o.order_id: o for o in (resp.data or [])}
        except ApiException as e:
            logger.error(f"get_order_book failed: status={e.status} body={e.body}")
            return {}

    OPEN_STATUSES = {"open", "trigger pending", "after market order req received",
                      "validation pending", "put order req received", "modify pending",
                      "modify after market order req received"}

    def cancel_unfilled(self, order_ids: dict) -> dict:
        """
        order_ids: {ticker: order_id}. Cancels anything still open, leaves
        filled orders alone. Returns {ticker: OrderData} with the final
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
            if o.status in self.OPEN_STATUSES:
                try:
                    self.order_v3.cancel_order(order_id)
                    logger.info(f"CANCELLED unfilled entry: {ticker} order_id={order_id} "
                                f"(was {o.status}, filled_qty={o.filled_quantity})")
                except ApiException as e:
                    logger.error(f"Cancel FAILED: {ticker} order_id={order_id} "
                                 f"status={e.status} body={e.body}")
        return final

    # ── Exit ─────────────────────────────────────────────────────

    def place_exit(self, position: dict) -> str:
        """
        Market-order exit for a filled position dict:
        {ticker, instrument_key, direction, entry_filled_qty}.
        Returns order_id or None.
        """
        ticker = position["ticker"]
        direction = position["direction"]
        qty = position["entry_filled_qty"]
        transaction = "SELL" if direction == "long" else "BUY"
        product = self.long_product if direction == "long" else self.short_product

        body = upstox_client.PlaceOrderV3Request(
            quantity=qty,
            product=product,
            validity="DAY",
            price=0,
            instrument_token=position["instrument_key"],
            order_type="MARKET",
            transaction_type=transaction,
            disclosed_quantity=0,
            trigger_price=0,
            is_amo=False,
            tag="overnight_reversal_exit",
        )
        try:
            resp = self.order_v3.place_order(body)
            order_id = str(resp.data.order_id)
            logger.info(f"EXIT placed: {ticker} {transaction} {qty} (MARKET) order_id={order_id}")
            return order_id
        except ApiException as e:
            logger.error(f"EXIT FAILED: {ticker} {transaction} {qty} "
                         f"status={e.status} body={e.body}")
            return None

    def place_exits(self, positions: list) -> dict:
        """positions: list of basket entries with entry_status filled/partial.
        Returns {ticker: order_id}."""
        results = {}
        for pos in positions:
            results[pos["ticker"]] = self.place_exit(pos)
        return results

    def confirm_fills(self, order_ids: dict, timeout_s: float = 20.0, poll_s: float = 2.0) -> dict:
        """
        Poll the order book until every order_id is out of an OPEN status or
        `timeout_s` elapses. Returns {ticker: OrderData} final snapshot.
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
                if o.status in self.OPEN_STATUSES:
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
