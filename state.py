"""
state.py  —  Persistent tracker for the day's basket of positions
─────────────────────────────────────────────────────────────────
Unlike a single pair/spread position, this strategy opens up to
n_long + n_short independent single-leg positions every morning and closes
all of them the same afternoon. This stores that whole basket to a JSON
file, keyed by trading date, so the bot can restart mid-day (e.g. between
the 09:15 entry pass and the 15:20 exit pass) without losing track of what
was filled, what qty, at what price, or which orders are still live.

Position schema (one entry per ticker in today's basket):
{
    "ticker":            "RELIANCE",
    "instrument_key":    "NSE_EQ|...",
    "direction":         "long" | "short",
    "qty":               int,
    "signal_price":      float,   # price used to rank + size the trade
    "entry_order_id":    str | null,
    "entry_limit_price": float | null,
    "entry_status":      "pending" | "filled" | "partial" | "cancelled" | "rejected",
    "entry_filled_qty":  int,
    "entry_fill_price":  float | null,
    "exit_order_id":     str | null,
    "exit_status":       "pending" | "filled" | "rejected" | "closed_manually" | null,
    "exit_fill_price":   float | null,
}
"""

import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)


class BasketState:
    def __init__(self, state_file: str):
        self.state_file = state_file
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    s = json.load(f)
                logger.info(f"Loaded basket state for {s.get('date')}: "
                            f"{len(s.get('positions', {}))} position(s)")
                return s
            except Exception as e:
                logger.warning(f"State file corrupt, resetting: {e}")
        return self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"date": None, "capital": None, "positions": {}}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    # ── Day lifecycle ────────────────────────────────────────────

    @property
    def today_str(self) -> str:
        return date.today().isoformat()

    def is_today(self) -> bool:
        return self._state.get("date") == self.today_str

    def start_new_day(self, capital: float):
        """Wipe any stale prior-day basket and start today's fresh."""
        self._state = {"date": self.today_str, "capital": capital, "positions": {}}
        self._save()
        logger.info(f"Started new basket for {self.today_str}, capital={capital:,.2f}")

    def ensure_today(self, capital: float):
        """Start a fresh basket unless today's is already in progress."""
        if not self.is_today():
            self.start_new_day(capital)

    @property
    def capital(self):
        return self._state.get("capital")

    @property
    def positions(self) -> dict:
        return self._state.get("positions", {})

    @property
    def is_empty(self) -> bool:
        return len(self.positions) == 0

    def has_open_legs(self) -> bool:
        """Any position still holding shares that need to be exited."""
        return any(
            p["entry_status"] in ("filled", "partial")
            and p.get("exit_status") not in ("filled", "closed_manually")
            for p in self.positions.values()
        )

    # ── Entry side ───────────────────────────────────────────────

    def add_planned_position(self, ticker, instrument_key, direction, qty, signal_price):
        self._state["positions"][ticker] = {
            "ticker": ticker,
            "instrument_key": instrument_key,
            "direction": direction,
            "qty": qty,
            "signal_price": signal_price,
            "entry_order_id": None,
            "entry_limit_price": None,
            "entry_status": "pending",
            "entry_filled_qty": 0,
            "entry_fill_price": None,
            "exit_order_id": None,
            "exit_status": None,
            "exit_fill_price": None,
        }
        self._save()

    def record_entry_order(self, ticker, order_id, limit_price):
        pos = self.positions.get(ticker)
        if pos is None:
            logger.warning(f"record_entry_order: {ticker} not in today's basket")
            return
        pos["entry_order_id"] = order_id
        pos["entry_limit_price"] = limit_price
        self._save()

    def record_entry_result(self, ticker, status, filled_qty=0, fill_price=None):
        pos = self.positions.get(ticker)
        if pos is None:
            logger.warning(f"record_entry_result: {ticker} not in today's basket")
            return
        pos["entry_status"] = status
        pos["entry_filled_qty"] = filled_qty
        pos["entry_fill_price"] = fill_price
        self._save()

    # ── Exit side ────────────────────────────────────────────────

    def record_exit_order(self, ticker, order_id):
        pos = self.positions.get(ticker)
        if pos is None:
            logger.warning(f"record_exit_order: {ticker} not in today's basket")
            return
        pos["exit_order_id"] = order_id
        pos["exit_status"] = "pending"
        self._save()

    def record_exit_result(self, ticker, status, fill_price=None):
        pos = self.positions.get(ticker)
        if pos is None:
            logger.warning(f"record_exit_result: {ticker} not in today's basket")
            return
        pos["exit_status"] = status
        pos["exit_fill_price"] = fill_price
        self._save()
