import os
import glob
import pandas as pd
import numpy as np
import datetime as dt

from metrics import plot_performance, print_summary
from overnight_reversal import demean_cross_sectionally

import yfinance as yf

TICKERS = [

    "63MOONS.NS", "AARTIIND.NS", "AARTIPHARM.NS", "AAVAS.NS", "ACMESOLAR.NS",
    "AETHER.NS", "AFFLE.NS", "AIAENG.NS", "AJANTPHARM.NS", "ALKYLAMINE.NS",
    "ANGELONE.NS", "APARINDS.NS", "APTUS.NS", "ARIHANTCAP.NS", "ARVIND.NS",
    "ARVINDFASN.NS", "ARVSMART.NS", "ASHAPURMIN.NS", "ASHOKA.NS", "ASTRAL.NS",
    "ATUL.NS", "ATULAUTO.NS", "AUBANK.NS", "AURIONPRO.NS", "AWFIS.NS",
    "AZAD.NS", "BALRAMCHIN.NS", "BANCOINDIA.NS", "BATAINDIA.NS", "BECTORFOOD.NS",
    "BLS.NS", "BLUEDART.NS", "BOMDYEING.NS", "BORORENEW.NS", "BRIGADE.NS",
    "BSE.NS", "BSOFT.NS", "CAMPUS.NS", "CAMS.NS", "CANFINHOME.NS",
    "CAPACITE.NS", "CAPLIPOINT.NS", "CARYSIL.NS", "CDSL.NS", "CERA.NS",
    "CHOICEIN.NS", "CHOLAFIN.NS", "CHOLAHLDNG.NS", "CLEAN.NS", "COCHINSHIP.NS",
    "CONCORDBIO.NS", "CRAFTSMAN.NS", "CREDITACC.NS", "CSBBANK.NS", "CUB.NS",
    "CYIENT.NS", "CYIENTDLM.NS", "DATAMATICS.NS", "DATAPATTNS.NS", "DCBBANK.NS",
    "DCMSHRIRAM.NS", "DEEPAKFERT.NS", "DEEPAKNTR.NS", "DELHIVERY.NS", "DEVYANI.NS",
    "DIXON.NS", "ECLERX.NS", "EDELWEISS.NS", "ELECON.NS", "ELECTCAST.NS",
    "EMCURE.NS", "ENDURANCE.NS", "ENGINERSIN.NS", "EPL.NS", "EQUITASBNK.NS",
    "ERIS.NS", "FACT.NS", "FIEMIND.NS", "FILATEX.NS", "FIVESTAR.NS",
    "FSL.NS", "FUSION.NS", "GABRIEL.NS", "GALLANTT.NS", "GANDHAR.NS",
    "GENESYS.NS", "GESHIP.NS", "GLAND.NS", "GLOBUSSPR.NS", "GMBREW.NS",
    "GNA.NS", "GNFC.NS", "GOKEX.NS", "GOKULAGRO.NS", "GOODLUCK.NS",
    "GPIL.NS", "GPPL.NS", "GRANULES.NS", "GRINDWELL.NS", "GRSE.NS",
    "GSFC.NS", "HAPPSTMNDS.NS", "HAPPYFORGE.NS", "HARIOMPIPE.NS", "HERITGFOOD.NS",
    "HFCL.NS", "HGINFRA.NS", "HIKAL.NS", "HINDCOPPER.NS", "HINDOILEXP.NS",
    "HINDZINC.NS", "HLEGLAS.NS", "HOMEFIRST.NS", "HSCL.NS", "HUDCO.NS",
    "IDEAFORGE.NS", "IDFCFIRSTB.NS", "IEX.NS", "IIFL.NS", "INDIAGLYCO.NS",
    "INDRAMEDCO.NS", "INDSWFTLAB.NS", "INOXWIND.NS", "INTELLECT.NS", "IRB.NS",
    "IRCON.NS", "IRFC.NS", "ITI.NS", "JAIBALAJI.NS", "JAICORPLTD.NS",
    "JAMNAAUTO.NS", "JINDALSAW.NS", "JISLJALEQS.NS", "JKPAPER.NS", "JMFINANCIL.NS",
    "JNKINDIA.NS", "JUBLFOOD.NS", "JUBLPHARMA.NS", "JWL.NS", "JYOTICNC.NS",
    "KARURVYSYA.NS", "KAYNES.NS", "KFINTECH.NS", "KIMS.NS", "KIOCL.NS",
    "KIRLOSBROS.NS", "KIRLOSENG.NS", "KITEX.NS", "KNRCON.NS", "KPIGREEN.NS",
    "KPRMILL.NS", "KRBL.NS", "KSB.NS", "LATENTVIEW.NS", "LAURUSLABS.NS",
    "LLOYDSME.NS", "LTFOODS.NS", "LUMAXIND.NS", "LUMAXTECH.NS", "LUXIND.NS",
    "LXCHEM.NS", "MAHLOG.NS", "MANAPPURAM.NS", "MANINFRA.NS", "MAPMYINDIA.NS",
    "MARINE.NS", "MARKSANS.NS", "MAXHEALTH.NS", "MAZDOCK.NS", "MCX.NS",
    "MEDANTA.NS", "MEDPLUS.NS", "METROPOLIS.NS", "MOIL.NS", "MOTHERSON.NS",
    "MOTILALOFS.NS", "MTARTECH.NS", "MUNJALAU.NS", "MUTHOOTFIN.NS", "NATCOPHARM.NS",
    "NATIONALUM.NS", "NAVA.NS", "NAZARA.NS", "NBCC.NS", "NCC.NS",
    "NEOGEN.NS", "NETWEB.NS", "NETWORK18.NS", "NEULANDLAB.NS", "NEWGEN.NS",
    "NFL.NS", "NITINSPIN.NS", "NMDC.NS", "NOCIL.NS", "NRBBEARING.NS",
    "NUVAMA.NS", "ORIENTHOT.NS", "OSWALPUMPS.NS", "PAISALO.NS", "PARAGMILK.NS",
    "PARAS.NS", "PATELENG.NS", "PCBL.NS", "PENIND.NS", "PGEL.NS",
    "PNBHOUSING.NS", "PNCINFRA.NS", "POKARNA.NS", "POLYCAB.NS", "POONAWALLA.NS",
    "PRAJIND.NS", "PRESTIGE.NS", "PRICOLLTD.NS", "PRIVISCL.NS", "PRUDENT.NS",
    "PSPPROJECT.NS", "PURVA.NS", "PVRINOX.NS", "QUESS.NS", "RAILTEL.NS",
    "RATEGAIN.NS", "RATNAMANI.NS", "RAYMOND.NS", "RAYMONDLSL.NS", "RBA.NS",
    "RBLBANK.NS", "RCF.NS", "REDINGTON.NS", "RELAXO.NS", "RITES.NS",
    "RVNL.NS", "SAKSOFT.NS", "SALZERELEC.NS", "SANDHAR.NS", "SANGHVIMOV.NS",
    "SANSERA.NS", "SAPPHIRE.NS", "SARDAEN.NS", "SAREGAMA.NS", "SATIN.NS",
    "SBFC.NS", "SERVOTECH.NS", "SHARDACROP.NS", "SHILPAMED.NS", "SHRIPISTON.NS",
    "SHYAMMETL.NS", "SIGNATURE.NS", "SJS.NS", "SJVN.NS", "SKIPPER.NS",
    "SOBHA.NS", "SOLARINDS.NS", "SONACOMS.NS", "SONATSOFTW.NS", "SPLPETRO.NS",
    "SUBROS.NS", "SUDARSCHEM.NS", "SUNTECK.NS", "SUNTV.NS", "SUPRIYA.NS",
    "SURYODAY.NS", "SUVEN.NS", "SUZLON.NS", "SYNGENE.NS", "SYRMA.NS",
    "TANLA.NS", "TARC.NS", "TARIL.NS", "TARSONS.NS", "TATVA.NS",
    "TDPOWERSYS.NS", "TECHNOE.NS", "THANGAMAYL.NS", "THYROCARE.NS", "TIINDIA.NS",
    "TIPSMUSIC.NS", "TITAGARH.NS", "TRIDENT.NS", "TRITURBINE.NS", "TRIVENI.NS",
    "UFLEX.NS", "UJJIVANSFB.NS", "UNIMECH.NS", "UNIPARTS.NS", "VADILALIND.NS",
    "VASCONEQ.NS", "VIJAYA.NS", "VIMTALABS.NS", "WAAREEENER.NS", "WABAG.NS",
    "WEBELSOLAR.NS", "WELCORP.NS", "WELENT.NS", "WELSPUNLIV.NS", "WESTLIFE.NS",
    "WOCKPHARMA.NS", "YATHARTH.NS", "ZEEL.NS", "ZENSARTECH.NS", "ZENTEC.NS",
]


CAPITAL = 20_000.0
LEVERAGE = 5
N_LONG = 10
N_SHORT = 10
 
# ---------------------------------------------------------------------------
# Trading costs (NSE cash intraday, MIS/square-off product).
# ---------------------------------------------------------------------------
BROKERAGE_BPS = 0.0          # ~0.03% per executed side
EXCHANGE_TXN_BPS = 0.297     # NSE transaction charge, per side
SEBI_TURNOVER_BPS = 0.01     # SEBI turnover fee, per side
GST_RATE = 0.18              # GST on (brokerage + exchange txn + SEBI charges)
STT_BPS_SELL = 2.5           # STT on intraday equity: 0.025%, sell side only
STAMP_DUTY_BPS_BUY = 0.3     # stamp duty on intraday equity: 0.003%, buy side only
SLIPPAGE_BPS = 10.0          
 
def round_trip_cost_frac():
    per_side_bps = BROKERAGE_BPS + EXCHANGE_TXN_BPS + SEBI_TURNOVER_BPS
    per_side_bps *= (1 + GST_RATE)
    total_bps = (
        2 * per_side_bps           
        + STT_BPS_SELL              
        + STAMP_DUTY_BPS_BUY        
        + 2 * SLIPPAGE_BPS          
    )
    return total_bps / 10_000.0
 
INDEX_NAMES = {"NIFTY 50", "NIFTY BANK"} 
MAX_ABS_RETURN = 25.0  
MOMENTUM_FILTER_ABS_RETURN = 5.0  

# ========== DATA LOADER ==========
def load_data():
    """
    Download daily OHLCV data for all tickers in TICKERS using yfinance.
    Returns a dictionary {ticker: DataFrame} with localized Asia/Kolkata time.
    """
    print(f"Downloading daily data for {len(TICKERS)} tickers (756d)...")
    result = yf.download(tickers=TICKERS, period='126d', interval='1d', 
                         auto_adjust=True, group_by='ticker', progress=False)
     
    if result.empty:
        raise ValueError("No data downloaded from yfinance")
    
    out = {}
    
    # Handle Multi-Ticker MultiIndex Case
    if isinstance(result.columns, pd.MultiIndex):
        for ticker in TICKERS:
            if ticker in result.columns.levels[0]:
                df = result[ticker].copy()
                df.columns = [col.lower() for col in df.columns]
                out[ticker] = df.dropna(how='all')
    else:
        # Single ticker case
        df = result.copy()
        df.columns = [col.lower() for col in df.columns]
        ticker = TICKERS[0] if isinstance(TICKERS, list) else TICKERS
        out[ticker] = df.dropna(how='all')

    # Ensure Timezone is Asia/Kolkata for accurate time-matching
    for t, df in out.items():
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")
        # Normalize to date only since it's daily data
        df.index = df.index.normalize()
        out[t] = df
        
    return out
 
# ========== RETURNS BUILDER ==========
def build_returns(data_dict):
    """
    Takes the in-memory data dictionary from load_data().
    Returns (r_co, r_id) DataFrames, columns = tickers, index = date, in %:
      r_co = open / prior_session_close - 1   (overnight gap)
      r_id = close / open - 1                 (held during the day)
    """
    r_co, r_id = {}, {}
    
    for ticker, df in data_dict.items():
        if ticker in INDEX_NAMES:
            continue
        if df.empty:
            continue
            
        prior_close = df["close"].shift(1)
        entry_px = df["open"]
        exit_px = df["close"]
 
        aligned = pd.concat(
            [prior_close, entry_px, exit_px],
            axis=1, keys=["prior_close", "entry", "exit"],
        ).dropna()
 
        co = (aligned["entry"] / aligned["prior_close"] - 1.0) * 100.0
        idr = (aligned["exit"] / aligned["entry"] - 1.0) * 100.0
 
        bad_co = co.abs() > MAX_ABS_RETURN
        bad_id = idr.abs() > MAX_ABS_RETURN
        
        co.loc[bad_co] = np.nan
        idr.loc[bad_id] = np.nan
 
        r_co[ticker] = co
        r_id[ticker] = idr
 
    r_co_df = pd.DataFrame(r_co).sort_index()
    r_id_df = pd.DataFrame(r_id).sort_index()
    return r_co_df, r_id_df
 
# ========== BACKTEST ENGINE ==========
def build_positions(r_co_dm, r_co, n_long=N_LONG, n_short=N_SHORT,
                     momentum_filter=MOMENTUM_FILTER_ABS_RETURN):
    r_co = r_co.reindex(index=r_co_dm.index, columns=r_co_dm.columns)
    not_momentum = r_co.abs() <= momentum_filter
 
    valid = r_co_dm.notna() & not_momentum
    valid_count = valid.sum(axis=1)
    enough = valid_count >= (n_long + n_short)
 
    r_co_dm_filtered = r_co_dm.where(not_momentum)
    asc_rank = r_co_dm_filtered.rank(axis=1, method="first")                    
    desc_rank = r_co_dm_filtered.rank(axis=1, method="first", ascending=False)  
 
    enough_2d = pd.DataFrame(
        np.tile(enough.values.reshape(-1, 1), r_co_dm.shape[1]),
        index=r_co_dm.index, columns=r_co_dm.columns,
    )
 
    long_mask = asc_rank.le(n_long) & valid & enough_2d
    short_mask = desc_rank.le(n_short) & valid & enough_2d
    return long_mask, short_mask
 
def run_backtest(r_co_dm, r_co, r_id, leverage=LEVERAGE, n_long=N_LONG, n_short=N_SHORT,
                  apply_costs=True):
    long_mask, short_mask = build_positions(r_co_dm, r_co, n_long, n_short)
    common = r_co_dm.columns.intersection(r_id.columns)
    r_id_frac = r_id[common].reindex(r_co_dm.index) / 100.0

    long_ret = r_id_frac.where(long_mask[common]).mean(axis=1)
    short_ret = r_id_frac.where(short_mask[common]).mean(axis=1)
    n_long_actual = long_mask.sum(axis=1)
    n_short_actual = short_mask.sum(axis=1)

    net_ret = leverage * long_ret - leverage * short_ret

    if apply_costs:
        cost = round_trip_cost_frac()
        net_ret = net_ret - (2 * leverage * cost)

    out = pd.DataFrame({
        "n_long": n_long_actual, "n_short": n_short_actual,
        "long_ret": long_ret, "short_ret": short_ret, "net_ret": net_ret,
    })
    return out.dropna(subset=["net_ret"]), long_mask, short_mask

def print_leg_comparison(bt_gross, bt_net):
    """Print average returns breakdown: long leg vs short leg over the period."""
    avg_long_gross = bt_gross["long_ret"].mean()
    avg_short_gross = bt_gross["short_ret"].mean()
    avg_long_net = bt_net["long_ret"].mean()
    avg_short_net = bt_net["short_ret"].mean()

    print(f"\n{'='*70}")
    print("LEG COMPARISON (before costs)")
    print(f"{'='*70}")
    print(f"{'Avg Long Basket Return:':<30} {avg_long_gross:>15.2%}")
    print(f"{'Avg Short Basket Return:':<30} {avg_short_gross:>15.2%}")
    print(f"{'Long Contribution (levered):':<30} {LEVERAGE * avg_long_gross:>15.2%}")
    print(f"{'Short Contribution (levered):':<30} {-LEVERAGE * avg_short_gross:>15.2%}")
    print(f"{'Net (Long - Short):':<30} {LEVERAGE * avg_long_gross - LEVERAGE * avg_short_gross:>15.2%}")
    print()

    print(f"{'='*70}")
    print("LEG COMPARISON (after costs)")
    print(f"{'='*70}")
    print(f"{'Avg Long Basket Return:':<30} {avg_long_net:>15.2%}")
    print(f"{'Avg Short Basket Return:':<30} {avg_short_net:>15.2%}")
    print(f"{'Long Contribution (levered):':<30} {LEVERAGE * avg_long_net:>15.2%}")
    print(f"{'Short Contribution (levered):':<30} {-LEVERAGE * avg_short_net:>15.2%}")
    print(f"{'Net (Long - Short):':<30} {LEVERAGE * avg_long_net - LEVERAGE * avg_short_net:>15.2%}")

def print_last_day_trades(long_mask, short_mask, r_co, r_id):
    """Print the trades for the last day with stock symbols, r_co, r_id, and position direction."""
    last_date = long_mask.index[-1]
    last_long = long_mask.loc[last_date]
    last_short = short_mask.loc[last_date]
    last_r_co = r_co.loc[last_date]
    last_r_id = r_id.loc[last_date]

    print(f"\n{'='*80}")
    print(f"LAST DAY TRADES: {last_date.strftime('%Y-%m-%d')}")
    print(f"{'='*80}\n")

    long_stocks = last_long[last_long].index.tolist()
    short_stocks = last_short[last_short].index.tolist()

    if long_stocks:
        print("LONG POSITIONS:")
        print(f"{'Stock':<20} {'r_co (%)':<15} {'r_id (%)':<15}")
        print("-" * 50)
        for stock in long_stocks:
            r_co_val = last_r_co.get(stock, np.nan)
            r_id_val = last_r_id.get(stock, np.nan)
            print(f"{stock:<20} {r_co_val:>13.2f}% {r_id_val:>13.2f}%")
        print()

    if short_stocks:
        print("SHORT POSITIONS:")
        print(f"{'Stock':<20} {'r_co (%)':<15} {'r_id (%)':<15}")
        print("-" * 50)
        for stock in short_stocks:
            r_co_val = last_r_co.get(stock, np.nan)
            r_id_val = last_r_id.get(stock, np.nan)
            print(f"{stock:<20} {r_co_val:>13.2f}% {r_id_val:>13.2f}%")
        print()

    print(f"Total Long: {len(long_stocks)}  |  Total Short: {len(short_stocks)}")

def main():
    # 1. Download data to dictionary
    data_dict = load_data()

    # 2. Build returns directly from dictionary
    r_co, r_id = build_returns(data_dict)

    print(f"Tickers processed: {len(r_co.columns)}")
    print(f"Trading days: {r_co.shape[0]}")
    print("Entry: Daily Open  |  Exit: Daily Close")

    r_co_dm, _ = demean_cross_sectionally(r_co)

    cost_bps = round_trip_cost_frac() * 10_000
    print(f"\nCosts: {cost_bps:.1f} bps round trip per leg")

    bt_gross, long_mask_gross, short_mask_gross = run_backtest(r_co_dm, r_co, r_id, leverage=LEVERAGE, apply_costs=False)
    bt, long_mask, short_mask = run_backtest(r_co_dm, r_co, r_id, leverage=LEVERAGE, apply_costs=True)

    print(f"\nCapital: ${CAPITAL:,.0f}/day (fixed notional, not compounded)")
    print(f"Leverage: {LEVERAGE}x long / {LEVERAGE}x short (gross {2 * LEVERAGE}x)\n")

    print("\n--- Gross (before costs) ---")
    print_summary(bt_gross["net_ret"], compounding=False)

    print("\n--- Net (after costs) ---")
    print_summary(bt["net_ret"], compounding=False)
    print(bt['net_ret'].iloc[-15:])

    print_leg_comparison(bt_gross, bt)
    print_last_day_trades(long_mask, short_mask, r_co, r_id)

    plot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_backtest_performance.png")
    plot_performance(bt["net_ret"], bt_gross["net_ret"], capital=CAPITAL, save_path=plot_path, compounding=False, r_co=r_co, r_id=r_id)
    print(f"\nSaved performance plot to: {plot_path}")
 
if __name__ == "__main__":
    main()


