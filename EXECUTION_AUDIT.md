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

## Addendum (2026-09-04) — read-timeout retries multiplying prep-loop latency

Reported: log spam of `ReadTimeoutError` / urllib3 "Retrying" warnings from
`fetch_prev_closes()`'s historical-candle calls to Upstox.

**Root cause**: `upstox_client`'s `RESTClientObject` builds its
`urllib3.PoolManager` with no explicit `retries=` kwarg, so every request
silently falls back to urllib3's own global default: `Retry(total=3)`.
Our own `REQUEST_TIMEOUT_S=15` (in `live_engine.py`) only bounds a single
attempt — urllib3 retries that same 15s timeout underneath it, up to 3
attempts total, before our code ever sees a failure. One slow/degraded
ticker can therefore cost 30-45+ seconds instead of the intended 15s cap.
`fetch_prev_closes()` makes this call sequentially for all ~300+ tickers
in the universe, during the narrow pre-market prep window
(`TIMING.prep_start` → `TIMING.market_open`) — a run of bad-luck timeouts
on a rough network morning could eat meaningfully into, or blow past,
`market_open`, directly threatening (or eliminating) the 5-minute entry
window that follows it.

**Fixes**:
- `auth.py.get_analytics_client()` now explicitly sets the Upstox client's
  connection-pool retry policy to `Retry(total=1, backoff_factor=0.3)`
  right after construction (before any request is made — urllib3 only
  applies `connection_pool_kw` to pools created after it's set). One
  retry, not zero: still recovers a one-off blip cheaply, but bounds
  worst case to ~2 attempts instead of urllib3's default 3. Verified this
  actually attaches to a freshly built `ApiClient`.
- `live_engine.SignalEngine.fetch_prev_closes()` now takes an optional
  `max_seconds` wall-clock budget for the *whole loop* (independent of
  the per-call timeout) — once spent, any tickers not yet attempted are
  dropped for today exactly like an individual fetch failure (loud error
  log, not fatal), rather than risking the entry window over prior-close
  data for names the loop hadn't reached yet. `bot.py` computes this as
  the actual seconds remaining until `market_open` minus a configurable
  safety buffer (`TIMING.prep_deadline_buffer_s`, default 60s) and passes
  it in; left `None` (e.g. a standalone/manual call) it's unbounded, same
  as before. Verified with a synthetic slow-API test: a tight budget
  correctly truncates the loop and reports exactly which tickers were
  dropped; an unbounded call still fetches everything.

Net effect: worst case for the whole prep pass is now capped at roughly
`prep_deadline_buffer_s` seconds past whatever's already been spent when
the budget check fires, instead of being able to silently expand by
30-45s *per bad ticker* with no ceiling — the entry pass now always gets
to start on time (with a possibly-smaller universe on a bad network
morning) rather than risk starting late or not at all.

## Addendum (2026-09-04, later same day) — two bot.py instances ran concurrently

Reported: "tmux bot crashed again" after the SIGHUP fix was already
deployed. The `logs/bot.log` tail told a different story than another
crash.

**What actually happened**: the log showed everything happening in
duplicate — two different "Fetching open prices (LTP) for N ticker(s)"
counts (123 vs 122) seconds apart, two different `fetch_prev_closes`
budget-exhausted lines with different elapsed times (635s vs 427s), two
back-to-back `── ENTRY PASS ──` lines. A single process cannot produce
this: `run_trading_day()` is one blocking synchronous call, it can't
re-enter itself mid-execution. This was **two separate `bot.py` processes
running at the same time** against the same live Kotak account, each
independently logging in, fetching signals off slightly different timing,
and both heading toward placing live entry orders for the same strategy.
Likely mechanism: the earlier "crash" was actually a tmux client
*disconnect* (session/process still alive on the server), not a process
death — starting a fresh `python bot.py` without confirming the old one
was actually gone produced two live instances. Beyond duplicate/doubled
orders, this also risks a fresh Kotak login silently invalidating the
other process's session (Kotak typically allows one active session per
account), which could make that process's *exit* orders later in the day
silently fail — a position that never gets flattened.

**Immediate action taken**: told the user to check `pgrep -af bot.py` on
the VPS, kill duplicates, and verify the actual broker-side order
book/positions directly (not the bot's local `state/position.json`, which
two processes writing to the same file concurrently could have corrupted
via a last-writer-wins race) before doing anything else. This needed a
human with account access to confirm/remediate — it is a live-trading
incident-response step, not a code change.

**Fix — a hard singleton lock, not just a warning**: `bot.py` now takes an
exclusive `flock` on `PATHS.lock_file` (`state/bot.lock` by default) at
startup, before login or any other work, and refuses to start at all
(loud `CRITICAL` log, exit code 1) if another instance already holds it.
`app.py` takes the exact same lock (same file) inside its
`@st.cache_resource`-wrapped `get_config()`, so the headless bot and the
Streamlit UI can't drive the same account simultaneously either, closing
the identical class of bug across both front doors.

Deliberately `flock`, not a hand-checked PID file: a PID file can go
stale forever if a process dies uncleanly (crash, `kill -9`, OOM) with
nothing left to clean it up, which would then block every future
legitimate start. `flock`'s lock is tied to the open file descriptor and
is released by the OS the instant that descriptor closes, however the
process ends — verified directly:
- idempotent within one process (calling it twice, e.g. across Streamlit
  reruns, doesn't self-deadlock);
- a second process attempting to start while the first holds the lock is
  correctly refused (exit code 1) — reproduces exactly today's incident
  and blocks it;
- after `kill -9`-ing the holder (simulating the unclean-crash case that
  matters most, not just a clean shutdown), a fresh process successfully
  re-acquires the lock immediately — no stale-lock deadlock after a real
  crash.

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

## Addendum (2026-09-07) — streamed open-price capture + parallel IOC entry ladder

Follow-up request: items 2 and 6 above (entries were a single flat-buffer
LIMIT order resting up to `entry_cutoff`, and entry price is an LTP
snapshot pulled a few seconds after `market_open` rather than the literal
open print) were both still costing real, if unquantified, edge on a
strategy whose reversal signal decays through the day — every second spent
either fetching the open price or waiting for a resting order to fill is a
second not spent capturing the move. Exit timing (item 5) was explicitly
**not** touched this pass — the user was clear the reversal is a morning
phenomenon and pushing `exit_start`/`exit_deadline` later to chase more of
the close isn't wanted; conservative exit timing stays as-is.

**1. Streamed open-price capture** (`live_engine.LiveQuoteStreamer`,
`SignalEngine.start_streaming/stop_streaming/_capture_open_quotes`):
`bot.py` now opens an Upstox WebSocket subscription (`MarketDataStreamerV3`,
`mode="full"`) for today's tradeable universe right after
`fetch_prev_closes()` — still well before `market_open` — instead of
`build_signals()` firing one bulk REST LTP call after the open unblocks.
Ticks (and top-of-book bid/ask) are already accumulating in an in-memory
cache by 09:15, so there's no request-response round trip on the critical
path between "market opens" and "we know the price." `build_signals()`
waits up to `TIMING.open_capture_window_s` (default 4s) for stragglers,
then falls back to the old bulk REST LTP call for just whatever's still
missing — full REST fallback also kicks in automatically if the stream
never connects at all, so this degrades gracefully rather than being a
single point of failure. Verified (unit-level, not live market hours):
message-parsing against a realistic v3 "full" feed payload, and the
missing-ticker → REST-fallback wiring, both behave as designed.

**2. Parallel IOC entry ladder replaces the resting LIMIT order**
(`execution.Executor.place_entry_basket`): Kotak's API supports
`validity="IOC"` for NSE cash equity (confirmed against the installed
`kotakneoapi` 3.0.1 SDK's own request validation) — an IOC order either
fills (fully/partially) or is cancelled by the exchange essentially
immediately, unlike the old `validity="DAY"` order that could legitimately
sit open for the full `market_open`→`entry_cutoff` window (up to 5
minutes) before the cancel pass ever looked at it. Entries are now fired
as a ladder of rungs (`STRATEGY.entry_ladder_bps`, default `10,25,45`):
each rung places an IOC order for every name still short its full quantity,
**in parallel** across a thread pool (`STRATEGY.entry_parallelism`, default
8) rather than the old sequential per-name loop, then polls briefly
(`entry_ladder_poll_interval_s`/`_timeout_s`) for the (near-instant) result
before moving to the next, wider rung. A name still short after the last
rung is simply left partially filled — the same outcome a cancelled
resting order used to produce, just discovered in seconds instead of up to
5 minutes. `run_cancel_pass` (~09:20) is unchanged in code but should now
normally be a no-op, since nothing is left resting.

**3. Liquidity-aware buffer**: each rung's limit price is anchored to the
*live* best bid/ask read from `LiveQuoteStreamer.get_touch()` at the
moment that rung fires — buy limit = ask × (1 + cushion), sell limit =
bid × (1 − cushion) — rather than a flat bps off the (by-then possibly
stale) signal price. Falls back to the old `signal_price × (1 ± cushion)`
behavior for any name the stream has no depth for at that moment.

**Concurrency caveat, not resolved here**: the previous rewrite
deliberately left order placement sequential specifically because Kotak
SDK thread-safety wasn't confirmed (see the 2026-09-03 addendum above).
Inspecting the currently-installed `kotakneoapi` 3.0.1 package shows it's
built on a pooled `httpx.Client` (documented thread-safe) with its own
rate limiter using a real `threading.Lock`, and `order_placing()` builds
fresh per-call request dicts off read-only session config rather than
mutating shared state — reasonable evidence this SDK version tolerates
concurrent callers, but this is inspection, not a live-verified guarantee.
**Watch the first several live parallel mornings against the actual Kotak
order book** (exactly one order per name per rung, no duplicates/drops)
before trusting `entry_parallelism` at higher values.

**Not done / explicitly out of scope this pass** (per direct instruction):
exit timing (item 5), signal-proportional position sizing, and a
transaction-cost-analysis report. `sizing.py` and the exit pass are
unchanged.
