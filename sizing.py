"""
sizing.py  —  Equal-capital-slot position sizing for the reversal basket
──────────────────────────────────────────────────────────────────────────
The day's capital is cut into `n_splits` equal slots (n_long + n_short,
normally 6 + 4 = 10). Every signal that fires gets exactly one slot,
regardless of how many longs vs shorts actually end up tradeable that day —
unused slots (missing data, price filtered out, capital too small for one
share) just sit in cash rather than being redistributed, so one day's
position sizing never depends on how many *other* names happened to qualify.

Per slot (base allocation):
    notional = slot_capital * leverage
    qty      = floor(notional / price)           (always rounds DOWN — never
                                                    borrow more than the slot
                                                    + leverage actually cover)
    remainder = notional - qty * price            (the bit of that slot's
                                                     notional the floor threw
                                                     away)

Both legs use the same flat STRATEGY.intraday_leverage (both sit on Upstox
product=I, cash-segment intraday — see execution.py). Nothing is held
overnight, so there's no MTF financing to pay for on the long leg either;
using one flat leverage on both sides is also what keeps each day's basket
dollar-neutral (equal notional long vs short per slot).

e.g. slot_capital=5,000, TCS @ Rs.3,000, leverage=5x → notional=25,000 →
qty = floor(25000/3000) = 8 shares, remainder = 25,000 - 24,000 = 1,000.

A name is dropped (not sized) if:
  - its live price exceeds STRATEGY.max_share_price, or
  - it's flagged exclude=true in instruments.json, or
  - it has no instrument_key resolved yet, or
  - the resulting qty rounds down to 0 (slot too small for one share).

Leftover-capital redistribution (0/1 knapsack)
────────────────────────────────────────────────
Every sized slot wastes its own `remainder` to the floor. Rather than let
each one evaporate independently, all remainders are pooled into one
`leftover_pool`, and that pool is used to buy at most ONE extra share for
some subset of the already-sized names (never more than one per name, so no
single name gets overweighted relative to the others — the basket stays
close to equal-weight/dollar-neutral).

The remainders are pure unspent cash — they were never "reserved" for their
own name, so once pooled they're fully fungible. Buying one extra share of
name i costs its full price_i out of the pool. We want the subset S of
names that maximises

    sum(price_i for i in S)          subject to   sum(...) <= leftover_pool

i.e. use as much of the pooled leftover as possible without ever going over
it. That's exactly 0/1 knapsack / subset-sum with value == weight == price_i
and capacity == leftover_pool — a classic, well-studied optimisation
problem, solved here exactly (not greedily) with a reachable-sums DP (see
`_bonus_share_subset`). With ~10-20 candidates a day the state space is
tiny, so exact DP is cheap. (A greedy cheapest-first pass is *not* used here
because it isn't guaranteed to find the subset that uses the most of the
pool — a classic knapsack pitfall.)

Why this can never blow the capital budget: let spend_base = sum(qty_i *
price_i) over the base (pre-bonus) allocation. By construction
leftover_pool = sum(notional_i) - spend_base = n_active * slot_notional -
spend_base. After granting bonus shares to subset S, spend_final =
spend_base + sum(price_i for i in S) <= spend_base + leftover_pool =
n_active * slot_notional <= n_splits * slot_notional = capital * leverage.
So spend is always <= the original budget; redistributing the leftover only
shrinks the amount left idle, it never creates new capital.
"""

import json
import logging
import os
from configparser import ConfigParser

logger = logging.getLogger(__name__)


def load_instruments(cfg: ConfigParser) -> dict:
    """Load the curated universe {ticker: {instrument_key, exclude}}."""
    path = cfg["STRATEGY"].get("instruments_file", "instruments.json")
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    with open(path) as f:
        data = json.load(f)
    return data["universe"]


def _bonus_share_subset(prices: list, pool: float) -> set:
    """
    Exact 0/1 knapsack / subset-sum: pick indices into `prices` (cost of one
    bonus share of that name) whose values sum to as close to `pool` as
    possible without exceeding it.

    Implemented as a reachable-sums DP: `reachable` maps every sum that can
    be built from the prices considered so far to the set of indices used
    to build it. Each new price either gets skipped or added to every
    previously-reachable sum (dropping any that would exceed the pool). The
    number of distinct reachable sums is bounded by min(2**n, pool), and n
    is one per day's tradeable name (~10-20), so this stays cheap in
    practice while still being exact (unlike a greedy cheapest-first pass,
    which can strand more of the pool unused — a classic knapsack pitfall).

    Amounts are worked in integer paise to avoid float drift accumulating
    across many additions inside the DP.
    """
    pool_paise = int(round(pool * 100))
    if pool_paise <= 0:
        return set()

    reachable = {0: frozenset()}
    for idx, price in enumerate(prices):
        p_paise = int(round(price * 100))
        if p_paise <= 0 or p_paise > pool_paise:
            continue
        for s, chosen in list(reachable.items()):
            ns = s + p_paise
            if ns <= pool_paise and ns not in reachable:
                reachable[ns] = chosen | {idx}

    best_sum = max(reachable)
    return set(reachable[best_sum])


class PositionSizer:
    def __init__(self, cfg: ConfigParser, instruments: dict = None):
        s = cfg["STRATEGY"]
        self.n_long           = int(s["n_long"])
        self.n_short          = int(s["n_short"])
        self.n_splits         = int(s.get("n_splits", self.n_long + self.n_short))
        self.max_share_price   = float(s["max_share_price"])
        self.intraday_leverage = float(s.get("intraday_leverage", 5.0))
        self.instruments       = instruments if instruments is not None else load_instruments(cfg)

    def size_positions(self, signals: list, capital: float) -> list:
        """
        signals: list of dicts, each {"ticker", "instrument_key", "direction"
                 ("long"/"short"), "price", "overnight_ret"} — as produced by
                 live_engine.SignalEngine.build_signals().
        capital: today's total capital (user-supplied each morning).

        Returns a list of dicts (subset of `signals` that sized to >=1 share)
        with "qty", "leverage", "slot_capital", "notional" added. Order is
        preserved from the input.
        """
        if self.n_splits <= 0:
            logger.error("n_splits must be > 0 — cannot size positions")
            return []

        slot_capital = capital / self.n_splits
        sized = []
        for sig in signals:
            ticker = sig["ticker"]
            price = sig["price"]
            info = self.instruments.get(ticker)

            if info is None:
                logger.warning(f"{ticker}: not in instruments.json — skipping")
                continue
            if info.get("exclude"):
                logger.info(f"{ticker}: excluded in instruments.json — skipping")
                continue
            if not sig.get("instrument_key"):
                logger.warning(f"{ticker}: no instrument_key resolved — skipping "
                                f"(run instrument_master.py)")
                continue
            if price is None or price <= 0:
                logger.warning(f"{ticker}: invalid price ({price}) — skipping")
                continue
            if price > self.max_share_price:
                logger.info(f"{ticker}: price {price:,.2f} exceeds max_share_price "
                            f"{self.max_share_price:,.2f} — skipping")
                continue

            leverage = self.intraday_leverage
            notional = slot_capital * leverage
            qty = int(notional // price)

            if qty < 1:
                logger.info(f"{ticker}: slot capital {slot_capital:,.2f} x {leverage}x "
                            f"leverage / price {price:,.2f} rounds down to 0 shares — skipping")
                continue

            remainder = notional - qty * price
            entry = dict(sig)
            entry.update({
                "leverage": leverage,
                "slot_capital": round(slot_capital, 2),
                "qty": qty,
                "notional": round(qty * price, 2),
                "_price": price,
                "_remainder": remainder,
            })
            sized.append(entry)

        # ── redistribute the pooled per-slot rounding leftover ──────────
        leftover_pool = sum(e["_remainder"] for e in sized)
        prices = [e["_price"] for e in sized]
        bonus_idx = _bonus_share_subset(prices, leftover_pool)

        bonus_spent = 0.0
        for i in bonus_idx:
            e = sized[i]
            e["qty"] += 1
            e["notional"] = round(e["qty"] * e["_price"], 2)
            bonus_spent += prices[i]

        for e in sized:
            del e["_price"], e["_remainder"]

        n_longs = sum(1 for p in sized if p["direction"] == "long")
        n_shorts = sum(1 for p in sized if p["direction"] == "short")
        logger.info(
            f"Sized {len(sized)}/{len(signals)} signals "
            f"({n_longs} long / {n_shorts} short) — slot_capital={slot_capital:,.2f} "
            f"(capital={capital:,.2f} / {self.n_splits} splits)"
        )
        logger.info(
            f"Leftover pool {leftover_pool:,.2f} → {len(bonus_idx)} bonus share(s) "
            f"granted, {bonus_spent:,.2f} utilized, {leftover_pool - bonus_spent:,.2f} "
            f"still unspent"
        )
        return sized
