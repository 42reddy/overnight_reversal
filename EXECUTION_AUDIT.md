# Execution audit — backtest vs. live parity (2026-09-01)

Triggered by: live returns (~0.5%, pre-costs) coming in far below the
backtest (`daily_data_backtest.py`, ~4%). This is a line-by-line comparison
of the live pipeline (`live_engine.py`, `sizing.py`, `execution.py`,
`bot.py`) against the backtest's actual math, with fixes applied where the
mismatch was a genuine live-side bug, and honest documentation where the
backtest's assumption simply isn't achievable with real capital/timing.

## Fixed (live-side bugs, now corrected)

### 1. Cross-sectional demeaning was disabled in the live ranking signal

`live_engine.py` had demeaning explicitly turned off:
```python
#mean_r = sum(r["r_co"] for r in rows) / len(rows)
for r in rows:
    r["overnight_ret"] = r["r_co"]  # demeaning disabled for now
```
so the basket was ranked and picked on **raw** overnight return. The
backtest ranks on `r_co_dm = r_co - mean(r_co across the universe that
day)` (`overnight_reversal.demean_cross_sectionally`, consumed by
`daily_data_backtest.build_positions`).

This matters a lot: on any day the index gaps as a whole (the common
case), raw-return ranking mostly just re-selects high-beta names moving
with the tape — a market-timing bet, not the idiosyncratic reversal the
strategy is supposed to harvest. The backtest's edge assumes the market
factor is stripped out first.

**Fix**: `live_engine.SignalEngine.build_signals()` now computes the
cross-sectional mean over every name with a usable price that morning,
demeans every `r_co` against it, and ranks/sizes off the demeaned value —
matching the backtest's `demean_cross_sectionally` exactly, including the
order of operations (mean computed *before* the momentum filter is
applied, same as `daily_data_backtest.build_positions`, so a handful of
momentum outliers don't skew the market-mean estimate away from what the
backtest would compute).

Sanity-checked with synthetic data where the whole "market" gaps up
2-3.5%: raw ranking would call every name a "winner"; demeaned ranking
correctly identifies the smallest-vs-tape movers as the long candidates
and the largest-vs-tape movers as the short candidates — the reversal bet
the strategy is actually supposed to make.

### 2. Entry orders were MARKET, not LIMIT

`execution.py Executor.place_entry()` sent `order_type="MKT"` for every
entry. A MARKET order has no worst-case price bound — on a thin NSE
mid/small-cap right at 09:15 (wide opening spread, order-book still
settling), the fill can land well outside the backtest's flat 10bps/side
slippage assumption (`daily_data_backtest.SLIPPAGE_BPS`).

**Fix**: entries are now LIMIT orders (`order_type="L"`, confirmed against
the installed `neo_api_client` SDK's `order_type_allowed_values`). Limit
price = signal price bumped by `STRATEGY.entry_limit_buffer_bps` (default
15bps) in the marketable direction — buy limit above the signal price,
sell limit below — rounded to a valid NSE tick (₹0.05). This bounds the
worst-case entry price close to the intended one instead of leaving it
unbounded. Anything unfilled by `entry_cutoff` (09:20) is now a
*meaningful* backstop via the existing `cancel_unfilled` pass, not the
rare race it was when entries were MARKET (MARKET orders resolve
essentially instantly; LIMIT orders can legitimately miss).

Exit orders are **unchanged (still MARKET)** — the exit pass has to
guarantee same-day flattening (MIS can't be carried), so fill certainty
matters more than a few bps of price there, and the backtest doesn't
model an "exit didn't fill" scenario either.

`entry_limit_buffer_bps` is a starting point (15bps, slightly wider than
the backtest's assumed 10bps to keep fill probability reasonable) —
tighten it once you've watched a few days of real fill rates.

### 3. No data-error guard on overnight return

The backtest drops any `|r_co| > 25%` as a bad print
(`daily_data_backtest.MAX_ABS_RETURN`) before it ever reaches ranking —
protection against a stale prior close, an unadjusted split/bonus, or a
bad tick producing a nonsense "overnight return." Live had no equivalent;
a bad print like that wouldn't just corrupt one name's own ranking, it
would also skew the cross-sectional mean used to demean every *other*
name that day.

**Fix**: added `STRATEGY.max_data_error_pct` (default 25%, matching the
backtest) — a name whose raw overnight move is at/beyond this is dropped
entirely (excluded from the mean too), separate from and outside the
5%-momentum filter (`max_overnight_move_pct`), which only excludes a name
from the *ranking pool*, not from the mean.

## Investigated, found NOT to be a live bug — the backtest is over-crediting itself

### 4. Leverage/capital allocation: the backtest silently assumes 2x the capital you actually have

The backtest's return math:
```python
long_ret  = r_id_frac.where(long_mask).mean(axis=1)     # equal-weighted avg return of the long names
short_ret = r_id_frac.where(short_mask).mean(axis=1)
net_ret   = leverage * long_ret - leverage * short_ret   # % of CAPITAL
```
reports `net_ret` as a percentage of `CAPITAL`, having applied `leverage`
independently to the **full** capital on the long book *and again*, fully,
on the short book (its own printout says so: `gross {2*LEVERAGE}x`). That
implies a total margin requirement of `2 * capital` — the long book's
margin and the short book's margin are both drawn in full against the same
`capital` figure.

`sizing.py` instead splits `capital` into `n_splits = n_long + n_short`
slots *before* applying leverage, so total margin used across the whole
basket sums to exactly `capital` (see the docstring's own margin proof).

I initially flagged this as a live-side sizing bug and proposed matching
the backtest by sizing each leg off `capital/n_long` or `capital/n_short`
instead of `capital/n_splits`. **This was wrong, and correctly rejected**:
doing that would require the account to actually post `2x` its stated
capital as margin simultaneously across the long and short books — real
brokers don't grant a netting/hedge margin benefit between an arbitrary
long book and short book of unrelated cash-equity names, so this margin
would have to be real, not notional. `sizing.py`'s current
`capital/n_splits` approach is the financially honest one: it's the
allocation that actually fits inside one pool of real capital.

**Conclusion — nothing changed here.** This is the backtest overstating
what's achievable, not live underperforming: with the stated `CAPITAL`
figure and a single margin pool, the honest, capital-feasible comparison
number is roughly **half** the backtest's reported return, not the full
4%. If real available margin is actually double the `capital` figure used
in the backtest/config (i.e. you're comfortable posting `2x` `capital` as
margin), that's a deliberate capital/risk decision to make explicitly
(e.g. by raising `STRATEGY.capital` to reflect the margin you're actually
willing to deploy) — not something to encode as a sizing formula change.

## Documented, not fixable without giving something up (broker/exchange constraints)

### 5. Exit happens ~30 minutes before the actual close

`daily_data_backtest.py`'s `r_id = close/open - 1` uses the exchange's
actual closing price (NSE cash closes ~15:30). Live's
`config.ini [TIMING] exit_start=14:55 / exit_deadline=15:00` flattens
everything a full ~30 minutes earlier, so live's realized intraday leg is
`price@~15:00 / price@open - 1`, not `close/open - 1` — whatever fraction
of that day's reversal move happens in the last half hour is captured by
the backtest and missed live.

This buffer is not arbitrary: MIS (intraday) products get broker-side
auto-square-off ahead of the exchange close, and most brokers stop
accepting fresh intraday exit orders somewhat before that. But per
`config.ini`'s own comment, **this specific timing was tuned against
Upstox's confirmed-live behavior, not Kotak's** — Kotak Neo's actual MIS
order-acceptance/auto-square-off cutoff has not been independently
re-verified for this rewrite (also flagged in `README.md`'s "Before going
live" section).

**Recommendation, not applied**: confirm Kotak Neo's actual cutoff (place
one manual test order near the close, or check Kotak Neo's published MIS
square-off policy for your segment) and, if it's later than 15:00, move
`exit_start`/`exit_deadline` closer to that real cutoff to recover more of
the day's move. This isn't changed in this pass because getting it wrong
in the other direction risks a penalty-priced forced square-off — it
needs your own broker-side confirmation, not a guess.

### 6. Entry price is an LTP snapshot, not the literal opening-auction print

The backtest's `entry_px = df["open"]` is the exchange's own recorded
opening print. Live's signal price is a bulk LTP pulled via API a few
seconds (up to the `wait_until` loop's 5s polling granularity, plus
network round-trip, plus — for later names in the sequential
`place_entries` loop — however long earlier orders in the loop took) after
`market_open` unblocks. It's a close proxy, not the same print, and
there's no way to get the literal exchange auction print without
co-located/tick-level infrastructure this bot doesn't have.

Not changed. Worth knowing about as a source of a few bps of unavoidable
timing noise, distinct from the LIMIT-order slippage fix in item 2 above.
One cheap partial mitigation, if entry latency for late-ranked names in a
30-name basket ever looks material in the logs: place entry orders
concurrently instead of strictly sequentially in `place_entries()` — not
done here since it's a latency optimization, not a correctness bug.

## Verified matching the backtest (no change)

- `n_long=16` / `n_short=14` — matches `daily_data_backtest.N_LONG/N_SHORT`.
- `intraday_leverage=5` — matches `daily_data_backtest.LEVERAGE`.
- `max_overnight_move_pct=5.0` — matches `MOMENTUM_FILTER_ABS_RETURN`.
- `n_splits=30 = n_long+n_short` — capital is split exactly as many ways
  as there are basket slots.
- `max_share_price` (live-only, no backtest equivalent) — a real capital
  constraint the backtest doesn't need to model (it assumes fractional/
  unconstrained sizing); left in place, not a source of return decay.

## Addendum (same day) — market-cap-weighted demeaning

Follow-up request: weight the demeaning mean by market cap instead of
treating every name in the universe equally, since a larger name actually
moves/represents "the tape" more than a microcap does. This is a
**deliberate, requested divergence from the backtest**, not a parity fix —
confirmed explicitly: the backtest's `demean_cross_sectionally` stays
equal-weighted; only `live_engine.py` changes. Until/unless the backtest
is updated to match, the 4% backtest figure and live's realized signal are
no longer testing the exact same ranking rule (on top of the already-known
capital/timing gaps in items 4-6 above).

**Implementation**:
- `instruments.json` gets a new `market_cap` field per ticker (rupees).
  Nothing in the live market-data path (Upstox) exposes this, so it's
  fetched offline via yfinance (`market_cap.py`, new file, run by hand
  periodically — market cap moves slowly enough that it doesn't need to be
  on the bot's daily startup path the way `instrument_key` resolution is).
- `live_engine.build_signals()` now computes `mean_r` as a
  `sqrt(market_cap)`-weighted average of `r_co` instead of a plain
  average. sqrt (not raw market cap) was the chosen damping — raw cap
  weighting would let a single large name in the universe dominate the
  mean outright; sqrt still weights larger names up meaningfully without
  one name swamping everyone else's demeaned signal.
- A ticker missing `market_cap` (not yet fetched, or fetch failed) is
  excluded from the mean *estimate* but still ranked against it — the
  basket doesn't shrink over a market-cap data gap. If **no** ticker has a
  market_cap on file, this falls back to the old equal-weighted mean for
  that day (loud warning, not a crash).
- Verified with synthetic data: a name with a much larger weight moving
  +3% while every small/mid name barely moves (+0.1-0.3%) correctly pulls
  the weighted mean far closer to +3% than an equal-weighted mean would —
  making the flat small/mid names look like large *relative* decliners
  (correct direction: they didn't participate in the "market's" move, so
  they're now the odd ones out), rather than the near-zero-signal result
  an equal-weighted mean would have given for the same data.
- Populated `instruments.json`'s `market_cap` for the actual 339-ticker
  universe via `python market_cap.py` (yfinance), so this is live-ready
  rather than a capability that silently falls back to equal-weighting
  every day for lack of data. Re-run it every few weeks to keep it fresh.

## Addendum (2026-09-03) — process crash during the entry pass

Reported: the tmux session running the bot crashed and closed right as
entry orders needed to be placed.

**Root cause**: `bot.py` never handled `SIGHUP`. A dropped SSH connection,
a crashed/closed tmux (or screen) session, or a closed terminal window all
send `SIGHUP` to whatever process is attached to that controlling
terminal. Python's default disposition for `SIGHUP` is immediate process
termination — no exception is raised, nothing in the code gets a chance
to catch it or clean up, and it can land at any instruction, including
mid-way through the entry loop. This matches the report exactly: the
process was killed by the OS itself, not by a bug in the trading logic
throwing an exception.

**Fix**: `bot.py` (and `app.py`, since `streamlit run app.py` is the same
kind of terminal-attached process) now calls `_ignore_terminal_hangup()`
at startup, which sets `SIGHUP` to `SIG_IGN`. A terminal disappearing can
no longer kill the process — functionally the same protection `nohup`
gives, but built into the process itself rather than depending on the
operator remembering to launch it that way. **This is a floor, not a
substitute** for real deployment hygiene: for a VPS, actually run under
`nohup ... & disown`, a detached (not closed) tmux/screen session, or —
better — a systemd service with `Restart=on-failure`, so the process is
fully independent of any terminal from the start and comes back on its
own after a crash or reboot, which ignoring one signal can't provide.

**Second, more dangerous gap this surfaced**: even before SIGHUP, ANY
crash mid-entry-loop (OOM kill, `kill -9`, power loss, this SIGHUP) could
leave an order that reached Kotak with no local record of it — a real
live position at the broker that both `cancel_unfilled` (no
`entry_order_id` on file) and the exit pass's per-ticker walk (never in
`state.positions`, or stuck at `entry_status="pending"` forever) would be
structurally blind to. It would never get flattened by the bot at all,
left entirely to the broker's own MIS auto-square-off as the only
backstop.

**Fix**: `run_exit_pass()` now also sweeps Kotak's live position book
(`get_net_positions()`) for any ticker with a nonzero position not
already accounted for by known state, and flattens those too as "orphan"
exits — loudly logged (`ORPHAN live position found...`) since there's no
`signal_price`/`entry_fill_price` on file to reconcile PnL against, but
flattened all the same rather than left open. This sweep now runs
unconditionally (previously the function returned early if local state
had zero known open positions, which would have skipped even checking the
broker in the pathological case where literally everything the bot
thought it knew was wrong). Verified with a synthetic test: a position
entirely absent from local state, with only a live broker-side quantity,
is now correctly discovered, flattened, and backfilled into
state/trade_log for the record.

**Also hardened**: `run_entry_pass()`'s per-name loop now catches
unexpected exceptions per-iteration (bookkeeping/disk-write errors, not
broker/network errors — `place_entry()` already handled those
internally) so one name's failure can't abort the rest of that morning's
basket. If that name's order actually reached the broker before the
failure, the new orphan sweep above will still find and flatten it later
even without a clean local record.

**Not done**: order placement in the entry loop is still sequential (one
network round-trip per name, back-to-back), which is a real if modest
source of return decay for later-ranked names in a large basket (see item
6 above) and was tempting to parallelize while in this code. Deliberately
left alone — thread-safety of the underlying `neo_api_client` session
under concurrent `place_order()` calls isn't documented/confirmed, and
`state.py`'s JSON read-modify-write isn't thread-safe either; getting
this wrong risks duplicate/dropped live orders or corrupted state, which
is a worse failure mode than a few seconds of timing dispersion. Only
worth revisiting with confirmed thread-safety guarantees from Kotak's SDK
docs/support.

## Net expectation after these fixes

Two of the three fixed items (demeaning, data-error guard) affect *which*
names get picked and are the more likely drivers of a large,
signal-quality gap; the LIMIT-order change affects *fill price* on
whichever names get picked. None of them are quantifiable in dollars
without live fills to compare against — watch the next several days'
`logs/trade_log.json` (`signal_price` vs `entry_fill_price` /
`entry_limit_price`, and fill-through rate on entries) to see the delta
directly. Separately, treat the backtest's headline 4% as a ~2%-of-capital
number for an apples-to-apples comparison against what a single real
margin pool can achieve (item 4), and expect a further, currently
unquantified haircut from the last-30-minutes-of-the-day gap (item 5)
until Kotak's real cutoff is confirmed and the exit window is tightened.
