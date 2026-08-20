"""
bot.py  —  Cross-Sectional Overnight-Reversal Basket Bot
═══════════════════════════════════════════════════════════════════════
Execution (orders, positions, cancels) runs entirely on Kotak Neo — see
execution.py / auth.py.get_kotak_client. Market data (prior close, LTP)
still comes from Upstox's long-lived Analytics Access Token — see
live_engine.py / auth.py.get_analytics_client — kept because Upstox's data
feed has proven more reliable than Kotak's for this. The two brokers never
overlap: Upstox is read-only data, Kotak is the only place an order is ever
placed, cancelled, or read back.

Runs forever (start it once, e.g. under systemd/nohup, and leave it up):
every trading day (Mon-Fri, minus NSE holidays — see holidays.json) it
wakes at STRATEGY.prep_start, runs one full day, then sleeps until the
next trading day's prep_start. Weekends and holidays are slept through in
one shot rather than polled every few seconds.

One trading day looks like:
  1. Login, refresh instrument tokens, fetch every universe name's prior
     close (idle time before the open — this is the slow part, so it's
     done before 09:15, not during the entry sprint).
  2. At 09:15: pull one bulk LTP snapshot, rank the universe by overnight
     return, size the basket (capital split into n_splits equal slots,
     leveraged flat at STRATEGY.intraday_leverage on both legs, rounded
     down to whole shares, backfilling from the next-ranked candidate if
     one can't be sized), and fire one MARKET entry order per name — longs
     on the biggest losers, shorts on the biggest winners.
  3. At 09:20: cancel anything still open (safety net; MARKET orders
     normally resolve immediately).
  4. At 15:00: reconcile against the broker's live position book (so a
     position closed/resized manually outside the bot is respected, not
     blindly re-exited into a reversed position) and MARKET-order out of
     whatever's actually still open, confirm fills, and finalize the day's
     PnL in the trade log.

Dry-run (local simulation, no orders sent) vs Live is controlled in
config.ini ([SANDBOX] enabled) — see execution.py's module docstring for
why "sandbox" now means a local simulation rather than a broker-side one.

Capital: since nothing is there to answer an interactive prompt at 3am,
persistent mode never prompts — it reads STRATEGY.capital from config.ini,
or the BOT_CAPITAL env var if set (checked fresh every trading day, so you
can change it between days without restarting the process).

Run:
    python bot.py            # persistent — runs forever, sleeps between days
    python bot.py --once     # single trading day then exit (old behaviour,
                              # still prompts interactively if a TTY is attached)
"""

import json
import logging
import os
import signal
import sys
import threading
import time as time_module
from configparser import ConfigParser
from datetime import date, datetime, timedelta, time as dt_time

import pytz
from dotenv import load_dotenv

import instrument_master
from auth import get_kotak_client, get_analytics_client
from execution import Executor
from live_engine import SignalEngine
from sizing import PositionSizer, load_instruments
from state import BasketState
from trade_log import TradeLogger

IST = pytz.timezone("Asia/Kolkata")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG & LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def load_config(path: str = "config.ini") -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(path)
    return cfg


def setup_logging(log_file: str):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )


logger = logging.getLogger("bot")


def _load_env():
    load_dotenv()


def _parse_hhmm(s: str) -> dt_time:
    h, m = map(int, s.strip().split(":"))
    return dt_time(h, m)


def _prompt_capital(default: float) -> float:
    """Interactive prompt when run from a real terminal; falls back to
    BOT_CAPITAL env var or config default under cron/systemd (no TTY),
    where input() would otherwise hang or raise EOFError."""
    env_capital = os.environ.get("BOT_CAPITAL")
    if env_capital:
        try:
            return float(env_capital)
        except ValueError:
            logger.warning(f"Couldn't parse BOT_CAPITAL='{env_capital}' as a number — "
                           f"using default {default:,.0f}")
            return default
    if not sys.stdin.isatty():
        logger.info(f"No TTY and BOT_CAPITAL not set — using default capital {default:,.0f}")
        return default
    raw = input(f"Today's capital (Enter for default {default:,.0f}): ").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Couldn't parse '{raw}' as a number — using default {default:,.0f}")
        return default


def wait_until(target: dt_time, label: str):
    """Blocks until IST clock reaches `target`, logging once at the start.
    No-ops immediately if that time has already passed today."""
    now = datetime.now(IST)
    target_dt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    remaining = (target_dt - now).total_seconds()
    if remaining <= 0:
        logger.info(f"{label} ({target.strftime('%H:%M')}) already passed — continuing immediately")
        return
    logger.info(f"Waiting until {label} ({target.strftime('%H:%M')} IST, "
               f"~{remaining / 60:.1f} min)...")
    while remaining > 0:
        time_module.sleep(min(remaining, 5.0))
        remaining = (target_dt - datetime.now(IST)).total_seconds()


def _resolve_capital_headless(default: float) -> float:
    """No input() — used by the persistent loop, which has no one at a
    keyboard to answer a prompt. Re-checked at the start of every trading
    day, so BOT_CAPITAL (or config.ini) can be changed between days without
    restarting the process."""
    env_capital = os.environ.get("BOT_CAPITAL")
    if env_capital:
        try:
            return float(env_capital)
        except ValueError:
            logger.warning(f"Couldn't parse BOT_CAPITAL='{env_capital}' as a number — "
                           f"using default {default:,.0f}")
    return default


# ══════════════════════════════════════════════════════════════════════════════
# HOLIDAY CALENDAR & SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

def load_holidays(path: str) -> set:
    """
    Reads {"holidays": ["YYYY-MM-DD", ...]} — NSE trading holidays — from
    `path`. Missing/unreadable file just means "no known holidays" (weekends
    are still always skipped); it's not fatal, since a stale/incomplete
    holiday list only costs a wasted wake-up on a day the market happens to
    be shut (fetch/order calls fail harmlessly and get logged), never a
    missed trading day.

    IMPORTANT: this file needs to be kept up to date by hand from the
    official NSE trading-holiday circular (nseindia.com) — see the "note"
    field inside holidays.json itself for which entries are safe fixed-date
    holidays vs. ones you need to fill in for lunar-calendar holidays
    (Holi, Diwali, Eid, etc.) that shift every year and aren't seeded here.
    """
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(path):
        logger.warning(f"Holiday file {path} not found — treating as no known holidays "
                       f"(weekends still skipped)")
        return set()
    try:
        with open(path) as f:
            raw = json.load(f)
        dates = raw["holidays"] if isinstance(raw, dict) else raw
        holidays = {datetime.strptime(d, "%Y-%m-%d").date() for d in dates}
        logger.info(f"Loaded {len(holidays)} holiday date(s) from {path}")
        return holidays
    except Exception as e:
        logger.warning(f"Couldn't parse holiday file {path} ({e}) — treating as no known holidays")
        return set()


def _is_trading_day(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def _should_run_today(now: datetime, cfg: ConfigParser, holidays: set) -> bool:
    """True if `now` falls on a trading day, before that day's exit deadline
    — i.e. there's still something worth doing today (entries, or exits for
    positions already filled by an earlier partial run)."""
    if not _is_trading_day(now.date(), holidays):
        return False
    exit_deadline = _parse_hhmm(cfg["TIMING"]["exit_deadline"])
    return now.time() < exit_deadline


def _next_prep_datetime(now: datetime, cfg: ConfigParser, holidays: set) -> datetime:
    """Next trading day's prep_start, strictly after `now`."""
    prep_t = _parse_hhmm(cfg["TIMING"].get("prep_start", cfg["TIMING"]["market_open"]))
    d = now.date()
    for _ in range(3660):  # ~10 years of calendar days — a sane upper bound, never hit in practice
        candidate = IST.localize(datetime.combine(d, prep_t))
        if _is_trading_day(d, holidays) and candidate > now:
            return candidate
        d += timedelta(days=1)
    raise RuntimeError("Could not find a next trading day within 10 years — check holidays.json")


def _sleep_until(target_dt: datetime, shutdown: threading.Event, chunk_s: float = 300.0):
    """Sleeps in `chunk_s` increments (instead of one long sleep) so the
    process wakes up periodically, can log that it's still alive, and can
    react to a shutdown signal promptly instead of blocking through it."""
    logged_eta = False
    while not shutdown.is_set():
        now = datetime.now(IST)
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            return
        if not logged_eta:
            logger.info(f"Sleeping until {target_dt.strftime('%Y-%m-%d %H:%M %Z')} "
                       f"(~{remaining / 3600:.1f}h away)")
            logged_eta = True
        shutdown.wait(min(remaining, chunk_s))


# ══════════════════════════════════════════════════════════════════════════════
# BOT
# ══════════════════════════════════════════════════════════════════════════════

class ReversalBot:
    def __init__(self, cfg: ConfigParser, capital: float):
        self.cfg = cfg
        self.capital = capital

        self.instruments = load_instruments(cfg)
        self.state = BasketState(cfg["PATHS"]["state_file"])
        self.trade_log = TradeLogger(cfg["PATHS"]["trade_log_file"])
        self.sizer = PositionSizer(cfg, instruments=self.instruments)

        self.api_client = None
        self.engine = None
        self.executor = None

    # ── Session ───────────────────────────────────────────────────

    def login(self):
        logger.info("=== Logging in: Kotak Neo (trading) + Upstox (market data) ===")

        # Orders, cancels, and position reads all go through Kotak Neo —
        # fresh TOTP+MPIN login every trading day (see auth.get_kotak_client).
        kotak_client = get_kotak_client(self.cfg)
        self.api_client = kotak_client

        # Market data always goes through Upstox's long-lived Analytics
        # Access Token (read-only, covers Market Data + Real-time/Streaming
        # APIs) — no daily login needed for it. Upstox never places orders;
        # that's Kotak's job exclusively.
        data_client = get_analytics_client(self.cfg)

        self.engine = SignalEngine(self.cfg, data_client, instruments=self.instruments)
        self.executor = Executor(kotak_client, self.cfg)
        logger.info("Login OK")

    def refresh_instrument_keys(self):
        cache_file = self.cfg["PATHS"].get("instrument_cache_file")
        instruments_file = self.cfg["STRATEGY"].get("instruments_file", "instruments.json")
        if not os.path.isabs(instruments_file):
            instruments_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), instruments_file)
        kwargs = {"instruments_file": instruments_file}
        if cache_file:
            kwargs["cache_file"] = cache_file
        resolved, missing = instrument_master.resolve_instrument_keys(**kwargs)
        if missing:
            logger.warning(f"{len(missing)} ticker(s) will be skipped today (no instrument_key): {missing}")
        # Reload so today's run has the freshly-resolved keys.
        self.instruments = load_instruments(self.cfg)
        self.sizer.instruments = self.instruments
        self.engine.instruments = self.instruments

    # ── Entry pass (~09:15) ─────────────────────────────────────────

    def run_entry_pass(self):
        logger.info("── ENTRY PASS ──")
        if self.state.positions:
            logger.warning(
                f"Today's basket already has {len(self.state.positions)} position(s) in "
                f"state — skipping entry pass to avoid double-entry (this run is likely a "
                f"restart after a crash). If the earlier pass genuinely failed before "
                f"placing anything, clear state/position.json's today entry manually."
            )
            return
        signals = self.engine.build_signals()
        if not signals:
            logger.error("No signals available at open — nothing to trade today")
            return

        sized = self.sizer.size_positions(signals, self.capital)
        if not sized:
            logger.error("No position sized to >=1 share — nothing to trade today")
            return

        for pos in sized:
            self.state.add_planned_position(
                pos["ticker"], pos["instrument_key"], pos["direction"],
                pos["qty"], pos["price"],
            )
            order_id, limit_price = self.executor.place_entry(pos)
            self.state.record_entry_order(pos["ticker"], order_id, limit_price)
            self.trade_log.log_entry_order(
                pos["ticker"], pos["direction"], pos["qty"], pos["leverage"],
                pos["price"], limit_price, order_id,
            )
            if order_id is None:
                self.state.record_entry_result(pos["ticker"], "rejected")
                self.trade_log.log_entry_result(pos["ticker"], "rejected", 0, None)

    # ── Cancel unfilled (~09:20) ────────────────────────────────────

    def run_cancel_pass(self):
        logger.info("── CANCEL-UNFILLED PASS ──")
        order_ids = {
            t: p["entry_order_id"] for t, p in self.state.positions.items()
            if p["entry_order_id"] and p["entry_status"] == "pending"
        }
        if not order_ids:
            logger.info("No pending entry orders to reconcile.")
            return

        final = self.executor.cancel_unfilled(order_ids)
        for ticker, o in final.items():
            status = self._resolve_status(o, self.state.positions[ticker]["qty"])
            self.state.record_entry_result(ticker, status, o.filled_quantity, o.average_price)
            self.trade_log.log_entry_result(ticker, status, o.filled_quantity, o.average_price)

        self._log_basket_snapshot()

    @staticmethod
    def _resolve_status(order_data, requested_qty) -> str:
        filled = order_data.filled_quantity or 0
        if filled >= requested_qty and requested_qty > 0:
            return "filled"
        if filled > 0:
            return "partial"
        if (order_data.status or "").strip().lower() in ("rejected", "rej"):
            return "rejected"
        return "cancelled"

    # ── Exit pass (~15:00, hard deadline 15:05) ─────────────────────

    def run_exit_pass(self):
        logger.info("── EXIT PASS ──")
        already_exited = {
            t for t, p in self.state.positions.items()
            if p.get("exit_status") in ("filled", "pending", "closed_manually")
        }
        if already_exited:
            logger.warning(
                f"Skipping re-exit for already-exited/in-flight position(s) "
                f"(restart safety): {sorted(already_exited)}"
            )
        open_positions = [
            dict(p, entry_filled_qty=p["entry_filled_qty"])
            for t, p in self.state.positions.items()
            if p["entry_status"] in ("filled", "partial") and p["entry_filled_qty"] > 0
            and t not in already_exited
        ]
        if not open_positions:
            logger.info("No filled positions to exit.")
            return

        # Reconcile against the broker's actual live position book before
        # placing anything. The bot's own state only knows what IT filled at
        # entry — if a position was closed (or resized) manually outside the
        # bot, blindly firing an exit sized off entry_filled_qty in the
        # entry's direction doesn't flatten anything: it OPENS A NEW POSITION
        # in the opposite direction on top of whatever's actually there.
        # Sizing and direction both come from the live net quantity instead.
        net_positions = self.executor.get_net_positions()

        to_exit = []
        for p in open_positions:
            ticker = p["ticker"]
            actual_qty = net_positions.get(ticker, 0)

            if actual_qty == 0:
                logger.warning(
                    f"{ticker}: bot state has an open {p['direction']} position "
                    f"(entry_filled_qty={p['entry_filled_qty']}) but the live broker "
                    f"position is flat — looks like it was already closed manually. "
                    f"Marking as closed, NOT placing an exit order (that would open a "
                    f"new position instead of closing one)."
                )
                self.state.record_exit_result(ticker, "closed_manually", None)
                self.trade_log.log_exit_result(ticker, "closed_manually", None)
                continue

            transaction = "SELL" if actual_qty > 0 else "BUY"
            qty = abs(actual_qty)
            expected_qty = p["entry_filled_qty"] if p["direction"] == "long" else -p["entry_filled_qty"]
            if actual_qty != expected_qty:
                logger.warning(
                    f"{ticker}: live position ({actual_qty}) doesn't match what the bot "
                    f"expected ({expected_qty}) — likely a partial manual close/add. "
                    f"Exiting exactly what's actually held ({transaction} {qty}), not "
                    f"the bot's original entry_filled_qty."
                )

            to_exit.append(dict(p, exit_qty=qty, exit_transaction=transaction))

        if not to_exit:
            logger.info("Nothing left to exit — every open position was already flat "
                        "at the broker.")
            return

        order_ids = self.executor.place_exits(to_exit)
        for ticker, order_id in order_ids.items():
            self.state.record_exit_order(ticker, order_id)
            self.trade_log.log_exit_order(ticker, order_id)

        deadline = self.cfg["TIMING"]["exit_deadline"]
        deadline_t = _parse_hhmm(deadline)
        now = datetime.now(IST)
        deadline_dt = now.replace(hour=deadline_t.hour, minute=deadline_t.minute, second=0, microsecond=0)
        timeout_s = max((deadline_dt - now).total_seconds(), 10.0)

        final = self.executor.confirm_fills(order_ids, timeout_s=timeout_s, poll_s=2.0)
        for ticker, o in final.items():
            status = "filled" if (o.filled_quantity or 0) > 0 else "rejected"
            self.state.record_exit_result(ticker, status, o.average_price)
            self.trade_log.log_exit_result(ticker, status, o.average_price)

        unresolved = set(order_ids) - set(final)
        for ticker in unresolved:
            logger.error(f"{ticker}: exit order never confirmed by {deadline} — check the "
                        f"Kotak Neo order book / positions manually.")

    # ── Logging ───────────────────────────────────────────────────

    def _log_basket_snapshot(self):
        lines = ["", f"  ┌── Basket Snapshot  ({self.state.today_str}) ──"]
        for ticker, p in self.state.positions.items():
            lines.append(
                f"  │  {ticker:<12} {p['direction']:<5} qty={p['qty']:<4} "
                f"entry_status={p['entry_status']:<10} filled={p['entry_filled_qty']}@"
                f"{p['entry_fill_price']}"
            )
        lines.append("  └────────────────────────────────────────────────────")
        logger.info("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# ONE TRADING DAY
# ══════════════════════════════════════════════════════════════════════════════

def run_trading_day(cfg: ConfigParser, capital: float):
    """Runs everything for a single trading day: login through final PnL.
    Blocks (via wait_until) at each phase boundary, so this doesn't return
    until the day's exit pass is done (or there's nothing left to do)."""
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  Overnight-Reversal Basket Bot — Kotak Neo   ║")
    logger.info("╚══════════════════════════════════════════════╝")

    now = datetime.now(IST)
    if now.weekday() >= 5:
        logger.info("Weekend — market closed, nothing to do.")
        return

    market_open = _parse_hhmm(cfg["TIMING"]["market_open"])
    entry_cutoff = _parse_hhmm(cfg["TIMING"]["entry_cutoff"])
    exit_start = _parse_hhmm(cfg["TIMING"]["exit_start"])
    exit_deadline = _parse_hhmm(cfg["TIMING"]["exit_deadline"])

    bot = ReversalBot(cfg, capital)
    bot.login()
    bot.refresh_instrument_keys()

    bot.state.ensure_today(capital)
    bot.trade_log.start_day(capital)

    if now.time() >= exit_deadline:
        logger.warning("Started after today's exit deadline — nothing left to do today.")
        return

    # Prior closes are the slow part (one historical-candle call per ticker) —
    # do it now, in whatever idle time is left before the open, not during
    # the 09:15 sprint.
    if now.time() < entry_cutoff:
        bot.engine.fetch_prev_closes()
        wait_until(market_open, "market open")
        bot.run_entry_pass()
        wait_until(entry_cutoff, "entry cutoff")
        bot.run_cancel_pass()
    else:
        logger.warning("Started after the entry cutoff — skipping today's entries "
                       "(any already-filled legs from an earlier run will still be exited).")
        if any(p["entry_status"] == "pending" for p in bot.state.positions.values()):
            logger.info("Found pending entry order(s) left over from an earlier run — "
                        "reconciling before the exit window.")
            bot.run_cancel_pass()

    wait_until(exit_start, "exit start")
    bot.run_exit_pass()

    bot.trade_log.finalize_day()
    bot.trade_log.log_portfolio_summary()
    logger.info("Day complete.")


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT LOOP — runs forever, sleeps between trading days
# ══════════════════════════════════════════════════════════════════════════════

def run_forever(cfg: ConfigParser):
    holidays = load_holidays(cfg["PATHS"].get("holidays_file", "holidays.json"))

    shutdown = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info(f"Received signal {signum} — will stop after the current "
                    f"step (may still be mid-trading-day; it will finish today's "
                    f"exit pass before exiting, not abandon open positions).")
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Persistent mode — running forever. Ctrl-C / SIGTERM to stop.")

    while not shutdown.is_set():
        now = datetime.now(IST)

        if _should_run_today(now, cfg, holidays):
            capital = _resolve_capital_headless(float(cfg["STRATEGY"]["capital"]))
            logger.info(f"{now.date()} is a trading day — capital={capital:,.2f}")
            try:
                run_trading_day(cfg, capital)
            except Exception:
                logger.exception("Unhandled error during today's trading run — "
                                 "will pick back up on the next trading day.")
            # Safety net: run_trading_day normally blocks until exit_start via
            # wait_until, so by the time it returns _should_run_today is
            # already False for today. This guards against a pathological
            # config (or an exception thrown before any wait_until ran)
            # turning that into a tight spin instead of an immediate re-check.
            shutdown.wait(1.0)
            continue  # re-check the clock; by now we're past exit_deadline

        target = _next_prep_datetime(now, cfg, holidays)
        reason = "weekend" if now.weekday() >= 5 else (
            "holiday" if now.date() in holidays else "after today's exit deadline"
        )
        logger.info(f"No trading to do right now ({reason}) — next prep window {target.date()}")
        _sleep_until(target, shutdown)

    logger.info("Shutdown complete.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _load_env()
    cfg = load_config()
    setup_logging(cfg["PATHS"]["log_file"])

    if "--once" in sys.argv:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            logger.info("Weekend — market closed, nothing to do.")
            return
        default_capital = float(cfg["STRATEGY"]["capital"])
        capital = _prompt_capital(default_capital)
        run_trading_day(cfg, capital)
    else:
        run_forever(cfg)


if __name__ == "__main__":
    main()
