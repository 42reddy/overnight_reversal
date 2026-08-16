"""
trade_log.py  —  Daily basket trade journal with PnL and portfolio tracking
──────────────────────────────────────────────────────────────────────────
Writes to logs/trade_log.json  (append-only per-day records + running totals).

Unlike a pair/spread trade, each trading day here is a BASKET of up to
n_long + n_short independent single-leg positions, all opened near 09:15
and all closed near 15:20-15:25. So the log is organized by day, with a
list of per-ticker legs underneath, rather than by individual open/close
pair-trade like the old stat-arb bot.

Day schema
──────────
{
  "date":      "YYYY-MM-DD",
  "capital":   50000.0,
  "positions": [
    {
      "ticker":            "RELIANCE",
      "direction":         "long" | "short",
      "qty":               int,             # sized qty (may exceed filled qty)
      "leverage":          float,
      "signal_price":      float,           # LTP used to rank + size
      "entry_limit_price": float | null,
      "entry_order_id":    str | null,
      "entry_time":        str | null,
      "entry_status":      "pending" | "filled" | "partial" | "cancelled" | "rejected",
      "entry_filled_qty":  int,
      "entry_fill_price":  float | null,
      "exit_order_id":     str | null,
      "exit_time":         str | null,
      "exit_status":       "pending" | "filled" | "rejected" | null,
      "exit_fill_price":   float | null,
      "pnl":               float | null      # None until both legs resolve
    }, ...
  ],
  "day_pnl":     float | null,
  "day_pnl_pct": float | null                 # day_pnl / capital
}
"""

import json
import logging
import os
from datetime import date, datetime
from typing import Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


class TradeLogger:
    def __init__(self, log_file: str):
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self._data = self._load()

    # ── Persistence ───────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    data = json.load(f)
                if "days" not in data:
                    logger.warning(
                        f"[TRADE LOG] {self.log_file} is in an old/incompatible schema "
                        f"(no 'days' key, likely from a prior strategy's bot) — starting "
                        f"fresh rather than crashing on it. The old file is left on disk "
                        f"until the next save overwrites it."
                    )
                else:
                    return data
            except Exception as e:
                logger.warning(f"[TRADE LOG] File corrupt, starting fresh: {e}")
        return {
            "days": [],
            "portfolio": {"total_pnl": 0.0, "days_traded": 0, "wins": 0, "losses": 0},
        }

    def _save(self):
        with open(self.log_file, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    @staticmethod
    def _now_str() -> str:
        return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    @staticmethod
    def _r(val, digits=2) -> Optional[float]:
        if val is None:
            return None
        try:
            return round(float(val), digits)
        except (TypeError, ValueError):
            return None

    def _today(self) -> Optional[dict]:
        today_str = date.today().isoformat()
        for d in reversed(self._data["days"]):
            if d["date"] == today_str:
                return d
        return None

    def _position(self, ticker: str) -> Optional[dict]:
        day = self._today()
        if day is None:
            return None
        for p in day["positions"]:
            if p["ticker"] == ticker:
                return p
        return None

    # ── Day lifecycle ────────────────────────────────────────────

    def start_day(self, capital: float):
        today_str = date.today().isoformat()
        if self._today() is not None:
            logger.info(f"[TRADE LOG] Day {today_str} already started — reusing")
            return
        self._data["days"].append({
            "date": today_str, "capital": self._r(capital),
            "positions": [], "day_pnl": None, "day_pnl_pct": None,
        })
        self._save()
        logger.info(f"[TRADE LOG] Day started {today_str}  capital={capital:,.2f}")

    # ── Entry side ───────────────────────────────────────────────

    def log_entry_order(self, ticker, direction, qty, leverage, signal_price,
                         entry_limit_price, order_id):
        day = self._today()
        if day is None:
            logger.warning("[TRADE LOG] log_entry_order called before start_day — skipping")
            return
        day["positions"].append({
            "ticker": ticker, "direction": direction, "qty": qty,
            "leverage": self._r(leverage, 2),
            "signal_price": self._r(signal_price, 2),
            "entry_limit_price": self._r(entry_limit_price, 2),
            "entry_order_id": order_id, "entry_time": self._now_str(),
            "entry_status": "pending" if order_id else "rejected",
            "entry_filled_qty": 0, "entry_fill_price": None,
            "exit_order_id": None, "exit_time": None,
            "exit_status": None, "exit_fill_price": None,
            "pnl": None,
        })
        self._save()

    def log_entry_result(self, ticker, status, filled_qty=0, fill_price=None):
        pos = self._position(ticker)
        if pos is None:
            logger.warning(f"[TRADE LOG] log_entry_result: {ticker} not found today")
            return
        pos["entry_status"] = status
        pos["entry_filled_qty"] = filled_qty
        pos["entry_fill_price"] = self._r(fill_price)
        self._save()
        logger.info(f"[TRADE LOG] Entry {ticker} {pos['direction'].upper()} "
                    f"status={status} filled={filled_qty}/{pos['qty']}@{fill_price}")

    # ── Exit side ────────────────────────────────────────────────

    def log_exit_order(self, ticker, order_id):
        pos = self._position(ticker)
        if pos is None:
            logger.warning(f"[TRADE LOG] log_exit_order: {ticker} not found today")
            return
        pos["exit_order_id"] = order_id
        pos["exit_time"] = self._now_str()
        pos["exit_status"] = "pending" if order_id else "rejected"
        self._save()

    def log_exit_result(self, ticker, status, fill_price=None):
        pos = self._position(ticker)
        if pos is None:
            logger.warning(f"[TRADE LOG] log_exit_result: {ticker} not found today")
            return
        pos["exit_status"] = status
        pos["exit_fill_price"] = self._r(fill_price)
        pos["pnl"] = self._r(self._calc_leg_pnl(pos))
        self._save()
        pnl_str = f"{pos['pnl']:+.2f}" if pos["pnl"] is not None else "N/A"
        logger.info(f"[TRADE LOG] Exit {ticker} status={status} @{fill_price}  PnL={pnl_str}")

    @staticmethod
    def _calc_leg_pnl(pos: dict) -> Optional[float]:
        entry = pos.get("entry_fill_price")
        exit_ = pos.get("exit_fill_price")
        qty = pos.get("entry_filled_qty") or 0
        if entry is None or exit_ is None or qty <= 0:
            return None
        if pos["direction"] == "long":
            return (exit_ - entry) * qty
        return (entry - exit_) * qty

    # ── Day close-out ────────────────────────────────────────────

    def finalize_day(self):
        """Compute day_pnl from whatever legs resolved and roll it into the
        running portfolio totals. Safe to call once after the exit pass."""
        day = self._today()
        if day is None:
            logger.warning("[TRADE LOG] finalize_day called with no day started")
            return None

        resolved = [p["pnl"] for p in day["positions"] if p["pnl"] is not None]
        unresolved = [p["ticker"] for p in day["positions"] if p["pnl"] is None]
        if unresolved:
            logger.warning(f"[TRADE LOG] {len(unresolved)} position(s) never resolved a "
                           f"PnL (missing entry/exit fill): {unresolved}")

        day_pnl = sum(resolved) if resolved else 0.0
        day["day_pnl"] = self._r(day_pnl)
        day["day_pnl_pct"] = self._r(day_pnl / day["capital"], 4) if day["capital"] else None

        portfolio = self._data["portfolio"]
        portfolio["days_traded"] += 1
        portfolio["total_pnl"] = round(portfolio["total_pnl"] + day_pnl, 2)
        if day_pnl > 0:
            portfolio["wins"] += 1
        elif day_pnl < 0:
            portfolio["losses"] += 1

        self._save()
        logger.info(
            f"[TRADE LOG] Day finalized {day['date']}  PnL={day_pnl:+.2f} "
            f"({day['day_pnl_pct']:+.2%} of capital)  "
            f"RunningPnL={portfolio['total_pnl']:+.2f}"
        )
        return day

    # ── Portfolio summary ─────────────────────────────────────────

    def get_days(self) -> list:
        """Every day recorded so far (open or finalized), oldest first."""
        return self._data.get("days", [])

    def get_portfolio_summary(self) -> dict:
        p = self._data["portfolio"]
        days_traded = p["days_traded"]
        day_pnls = [d["day_pnl"] for d in self._data["days"] if d.get("day_pnl") is not None]
        summary = {
            "days_traded": days_traded,
            "wins": p["wins"], "losses": p["losses"],
            "win_rate": round(p["wins"] / days_traded, 3) if days_traded > 0 else None,
            "total_pnl": p["total_pnl"],
        }
        if day_pnls:
            summary["best_day"] = round(max(day_pnls), 2)
            summary["worst_day"] = round(min(day_pnls), 2)
            summary["avg_day"] = round(sum(day_pnls) / len(day_pnls), 2)
            running = peak = max_dd = 0.0
            for pnl in day_pnls:
                running += pnl
                peak = max(peak, running)
                max_dd = min(max_dd, running - peak)
            summary["max_drawdown"] = round(max_dd, 2)
        return summary

    def log_portfolio_summary(self):
        s = self.get_portfolio_summary()
        wr = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "N/A"
        logger.info(
            f"[PORTFOLIO] Days={s['days_traded']}  W/L={s['wins']}/{s['losses']}  "
            f"WinRate={wr}  TotalPnL={s['total_pnl']:+.2f}  MaxDD={s.get('max_drawdown', 0.0):+.2f}"
        )
