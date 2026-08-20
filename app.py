"""
app.py  —  Streamlit UI for the overnight-reversal basket bot
────────────────────────────────────────────────────────────────
A thin front-end over bot.py's ReversalBot: you open this around 09:15,
click through login → fetch prior closes → entry → cancel-unfilled, close
the tab, come back around 15:20 and click exit. No process needs to stay
up in between — every state change (basket, fills, PnL) is persisted to
disk by state.py / trade_log.py exactly as it is for the headless bot.py,
so this reads/writes the same files and either front-end can pick up
where the other left off.

Run:
    streamlit run app.py
"""

import calendar
import json
import os
from datetime import date, datetime, time as dt_time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot import ReversalBot, load_config, setup_logging, _parse_hhmm, IST, _load_env
from state import BasketState
from trade_log import TradeLogger

# ── Palette (validated diverging/status colors — see dataviz skill) ────────
GOOD_TEXT = "#0ca30c"      # status: good (mode-invariant)
CRITICAL_TEXT = "#d03b3b"  # status: critical (mode-invariant)
BLUE = "#2a78d6"           # categorical slot 1 — single-series lines
GOOD_BG_LIGHT, GOOD_BG_DARK = "#e3f5e3", "#123a12"
CRIT_BG_LIGHT, CRIT_BG_DARK = "#fbe9e8", "#3a1616"
NEUTRAL_BG_LIGHT, NEUTRAL_BG_DARK = "#f0efec", "#232320"

st.set_page_config(page_title="Reversal Basket Bot", layout="wide")


# ══════════════════════════════════════════════════════════════════════════
# Session bootstrap — cheap, read-only objects rebuilt every rerun
# ══════════════════════════════════════════════════════════════════════════

@st.cache_resource
def get_config():
    _load_env()
    cfg = load_config()
    setup_logging(cfg["PATHS"]["log_file"])
    return cfg


cfg = get_config()

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.bot = None
    st.session_state.capital = float(cfg["STRATEGY"]["capital"])

# Read-only views of on-disk state work even before login, and reflect
# whatever bot.py (headless) or a prior app.py session already did today.
basket_state = BasketState(cfg["PATHS"]["state_file"])
trade_log = TradeLogger(cfg["PATHS"]["trade_log_file"])
sandbox = cfg["SANDBOX"].getboolean("enabled", fallback=True)


# ══════════════════════════════════════════════════════════════════════════
# Market phase
# ══════════════════════════════════════════════════════════════════════════

def market_phase(cfg):
    now = datetime.now(IST)
    t = now.time()
    market_open = _parse_hhmm(cfg["TIMING"]["market_open"])
    entry_cutoff = _parse_hhmm(cfg["TIMING"]["entry_cutoff"])
    exit_start = _parse_hhmm(cfg["TIMING"]["exit_start"])
    exit_deadline = _parse_hhmm(cfg["TIMING"]["exit_deadline"])

    if now.weekday() >= 5:
        return "weekend", "Weekend — market closed", "off"
    if t < market_open:
        return "pre_open", f"Pre-open — entry window opens at {market_open.strftime('%H:%M')}", "info"
    if t < entry_cutoff:
        return "entry_window", f"Entry window — cancel-unfilled at {entry_cutoff.strftime('%H:%M')}", "action"
    if t < exit_start:
        return "holding", f"Holding — exit window opens at {exit_start.strftime('%H:%M')}", "info"
    if t < exit_deadline:
        return "exit_window", f"Exit window — hard deadline {exit_deadline.strftime('%H:%M')}", "action"
    return "closed", "Past exit deadline — trading day over", "off"


phase_key, phase_label, phase_kind = market_phase(cfg)


# ══════════════════════════════════════════════════════════════════════════
# Sidebar — session
# ══════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("Session")
    if sandbox:
        st.success("DRY-RUN — simulated locally, no orders sent")
    else:
        st.error("LIVE — real orders on Kotak Neo")

    st.caption(f"IST now: {datetime.now(IST).strftime('%H:%M:%S')}")
    if st.button("Refresh"):
        st.rerun()

    st.session_state.capital = st.number_input(
        "Today's capital (₹)", min_value=0.0, step=1000.0,
        value=st.session_state.capital,
    )

    if not st.session_state.logged_in:
        if st.button("Login", type="primary"):
            with st.spinner("Logging in to Kotak Neo (trading) + Upstox (market data)..."):
                try:
                    bot = ReversalBot(cfg, st.session_state.capital)
                    bot.login()
                    bot.refresh_instrument_keys()
                    bot.state.ensure_today(st.session_state.capital)
                    bot.trade_log.start_day(st.session_state.capital)
                    st.session_state.bot = bot
                    st.session_state.logged_in = True
                    st.rerun()
                except Exception as e:
                    st.error(f"Login failed: {e}")
    else:
        st.success("Logged in")
        if st.button("Log out"):
            st.session_state.logged_in = False
            st.session_state.bot = None
            st.rerun()

    live_armed = True
    if not sandbox:
        st.markdown("---")
        live_armed = st.checkbox(
            "I confirm I want to place LIVE orders today",
            value=False,
            help="Required in LIVE mode before Entry/Exit buttons will do anything.",
        )

st.title("Overnight-Reversal Basket Bot")

if phase_kind == "action":
    st.warning(f"**{phase_label}**")
elif phase_kind == "off":
    st.info(f"**{phase_label}**")
else:
    st.info(f"**{phase_label}**")

tab_today, tab_calendar, tab_portfolio = st.tabs(["Today", "Calendar", "Portfolio"])


# ══════════════════════════════════════════════════════════════════════════
# TODAY
# ══════════════════════════════════════════════════════════════════════════

def _action_disabled():
    return not (st.session_state.logged_in and live_armed)


with tab_today:
    bot = st.session_state.bot

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if st.button("1. Fetch prior closes", disabled=_action_disabled(),
                     help="Pulls each name's last completed-session close. Do this "
                          "any time before the open — it's the slow call."):
            with st.spinner("Fetching prior closes..."):
                try:
                    n = len(bot.engine.fetch_prev_closes())
                    st.success(f"Prior close resolved for {n} tickers")
                except Exception as e:
                    st.error(f"Failed: {e}")

    with c2:
        if st.button("2. Run entry", disabled=_action_disabled(),
                     help="Ranks the universe on this morning's LTP vs prior close, "
                          "sizes the basket, and fires MARKET entry orders."):
            with st.spinner("Placing entries..."):
                try:
                    bot.run_entry_pass()
                    st.success("Entry pass complete — see basket below")
                except Exception as e:
                    st.error(f"Failed: {e}")

    with c3:
        if st.button("3. Cancel unfilled", disabled=_action_disabled(),
                     help="Cancels any entry order still open (normally ~09:20)."):
            with st.spinner("Cancelling unfilled entries..."):
                try:
                    bot.run_cancel_pass()
                    st.success("Cancel pass complete")
                except Exception as e:
                    st.error(f"Failed: {e}")

    with c4:
        if st.button("4. Run exit", disabled=_action_disabled(),
                     help="MARKET-orders out of every filled leg (normally ~15:20)."):
            with st.spinner("Exiting positions..."):
                try:
                    bot.run_exit_pass()
                    day = bot.trade_log.finalize_day()
                    st.success(f"Exit pass complete — day PnL "
                              f"₹{day['day_pnl']:+,.2f}" if day else "Exit pass complete")
                except Exception as e:
                    st.error(f"Failed: {e}")

    if not sandbox and not live_armed:
        st.caption("⚠️ Live mode — check the confirmation box in the sidebar to enable the buttons above.")
    elif not st.session_state.logged_in:
        st.caption("Log in from the sidebar to enable the buttons above.")

    st.subheader("Today's basket")
    positions = basket_state.positions
    if not positions:
        st.caption("No positions yet today.")
    else:
        rows = []
        for t, p in positions.items():
            live_pnl = None
            if p["entry_fill_price"] is not None and p["exit_fill_price"] is not None:
                qty = p["entry_filled_qty"]
                if p["direction"] == "long":
                    live_pnl = (p["exit_fill_price"] - p["entry_fill_price"]) * qty
                else:
                    live_pnl = (p["entry_fill_price"] - p["exit_fill_price"]) * qty
            rows.append({
                "Ticker": t, "Dir": p["direction"], "Qty": p["qty"],
                "Signal px": p["signal_price"], "Ranked at": p["entry_limit_price"],
                "Entry status": p["entry_status"], "Entry fill": p["entry_fill_price"],
                "Exit status": p["exit_status"], "Exit fill": p["exit_fill_price"],
                "PnL": live_pnl,
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style.map(
                lambda v: f"color: {GOOD_TEXT}" if isinstance(v, (int, float)) and v > 0
                else (f"color: {CRITICAL_TEXT}" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["PnL"],
            ),
            width="stretch", hide_index=True,
        )


# ══════════════════════════════════════════════════════════════════════════
# CALENDAR
# ══════════════════════════════════════════════════════════════════════════

def render_calendar_html(year: int, month: int, day_pnl: dict) -> str:
    cal = calendar.Calendar(firstweekday=0)  # Monday first
    weeks = cal.monthdayscalendar(year, month)
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    header = "".join(f"<div class='rb-cal-head'>{d}</div>" for d in weekday_names)
    cells = []
    for week in weeks:
        for day_num in week:
            if day_num == 0:
                cells.append("<div class='rb-cal-cell rb-cal-empty'></div>")
                continue
            d = date(year, month, day_num)
            pnl = day_pnl.get(d.isoformat())
            if pnl is None:
                cls, txt = "rb-cal-neutral", ""
            elif pnl > 0:
                cls, txt = "rb-cal-good", f"₹{pnl:+,.0f}"
            elif pnl < 0:
                cls, txt = "rb-cal-critical", f"₹{pnl:+,.0f}"
            else:
                cls, txt = "rb-cal-neutral", "₹0"
            cells.append(
                f"<div class='rb-cal-cell {cls}'>"
                f"<div class='rb-cal-daynum'>{day_num}</div>"
                f"<div class='rb-cal-pnl'>{txt}</div></div>"
            )
    grid = header + "".join(cells)

    return f"""
    <style>
      .rb-cal-grid {{
        display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      }}
      .rb-cal-head {{
        text-align: center; font-size: 12px; font-weight: 600;
        color: #898781; padding-bottom: 4px;
      }}
      .rb-cal-cell {{
        min-height: 64px; border-radius: 8px; padding: 6px 8px;
        background: {NEUTRAL_BG_LIGHT};
      }}
      .rb-cal-empty {{ background: transparent; }}
      .rb-cal-daynum {{ font-size: 12px; color: #52514e; }}
      .rb-cal-pnl {{ font-size: 13px; font-weight: 700; margin-top: 6px; }}
      .rb-cal-good {{ background: {GOOD_BG_LIGHT}; }}
      .rb-cal-good .rb-cal-pnl {{ color: {GOOD_TEXT}; }}
      .rb-cal-critical {{ background: {CRIT_BG_LIGHT}; }}
      .rb-cal-critical .rb-cal-pnl {{ color: {CRITICAL_TEXT}; }}
      @media (prefers-color-scheme: dark) {{
        .rb-cal-cell {{ background: {NEUTRAL_BG_DARK}; }}
        .rb-cal-daynum {{ color: #c3c2b7; }}
        .rb-cal-good {{ background: {GOOD_BG_DARK}; }}
        .rb-cal-critical {{ background: {CRIT_BG_DARK}; }}
      }}
    </style>
    <div class="rb-cal-grid">{grid}</div>
    """


with tab_calendar:
    days_data = trade_log.get_days()
    day_pnl_map = {d["date"]: d["day_pnl"] for d in days_data if d.get("day_pnl") is not None}

    today = date.today()
    available_months = sorted({(int(d[:4]), int(d[5:7])) for d in day_pnl_map} | {(today.year, today.month)})
    labels = [f"{calendar.month_name[m]} {y}" for y, m in available_months]
    default_idx = available_months.index((today.year, today.month))
    sel = st.selectbox("Month", options=list(range(len(available_months))),
                       format_func=lambda i: labels[i], index=default_idx)
    year, month = available_months[sel]

    st.markdown(render_calendar_html(year, month, day_pnl_map), unsafe_allow_html=True)
    st.caption("Green = profitable day · Red = losing day · Gray = no trades / no data")


# ══════════════════════════════════════════════════════════════════════════
# PORTFOLIO
# ══════════════════════════════════════════════════════════════════════════

with tab_portfolio:
    summary = trade_log.get_portfolio_summary()
    days_data = trade_log.get_days()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total PnL", f"₹{summary['total_pnl']:,.0f}")
    m2.metric("Days traded", summary["days_traded"])
    m3.metric("Win rate", f"{summary['win_rate']:.0%}" if summary["win_rate"] is not None else "—")
    m4.metric("Max drawdown", f"₹{summary.get('max_drawdown', 0.0):,.0f}")

    resolved_days = [d for d in days_data if d.get("day_pnl") is not None]
    if resolved_days:
        dates = [d["date"] for d in resolved_days]
        pnls = [d["day_pnl"] for d in resolved_days]
        cum = pd.Series(pnls).cumsum()
        first_capital = resolved_days[0]["capital"] or 0.0
        portfolio_value = first_capital + cum

        st.metric("Current portfolio value (baseline capital + cumulative PnL)",
                  f"₹{portfolio_value.iloc[-1]:,.0f}")

        fig_equity = go.Figure()
        fig_equity.add_trace(go.Scatter(
            x=dates, y=portfolio_value, mode="lines", line=dict(color=BLUE, width=2),
            name="Portfolio value",
        ))
        fig_equity.update_layout(
            title="Portfolio value over time", height=320,
            margin=dict(l=10, r=10, t=40, b=10),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#e1e0d9", tickprefix="₹"),
        )
        st.plotly_chart(fig_equity, width="stretch")

        bar_colors = [GOOD_TEXT if p >= 0 else CRITICAL_TEXT for p in pnls]
        fig_daily = go.Figure()
        fig_daily.add_trace(go.Bar(x=dates, y=pnls, marker_color=bar_colors, name="Day PnL"))
        fig_daily.update_layout(
            title="Day-by-day PnL", height=280,
            margin=dict(l=10, r=10, t=40, b=10),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#e1e0d9", tickprefix="₹"),
        )
        st.plotly_chart(fig_daily, width="stretch")
    else:
        st.caption("No finalized trading days yet.")
