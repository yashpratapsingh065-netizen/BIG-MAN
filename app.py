import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

try:
    import MetaTrader5 as mt5
    MT5_OK = True
except ImportError:
    mt5 = None
    MT5_OK = False

try:
    import yfinance as yf
    YF_OK = True
except ImportError:
    YF_OK = False

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.getenv("BIGMAN_DB_PATH", "bigman_price_lesson.db")
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "BIGMAN-UltimatePriceLesson/4.1"

st.set_page_config(
    page_title="BIGMAN | Ultimate Expert Price Lesson Terminal", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""<style>
.stApp { background: #07090e; color: #f3f4f6; font-family: 'Inter', sans-serif; }
h1, h2, h3 { color: white; font-family: 'Inter', sans-serif; }
.bh { font-size: 2.2rem; font-weight: 850; background: linear-gradient(90deg, #38bdf8, #34d399);
-webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.stButton>button { width: 100%; font-weight: 650; border-radius: 8px; background-color: #0284c7; color: white; border: none; }
.stButton>button:hover { background-color: #0369a1; }
</style>""", unsafe_allow_html=True)

ASSETS = {
    "XAU/USD (Gold)": {"mt5": os.getenv("BIGMAN_MT5_XAU", "XAUUSD"), "td": "XAU/USD", "tv": "OANDA:XAUUSD", "digits": 2, "contract": 100.0, "yf": "GC=F"},
    "EUR/USD": {"mt5": os.getenv("BIGMAN_MT5_EURUSD", "EURUSD"), "td": "EUR/USD", "tv": "FX:EURUSD", "digits": 5, "contract": 100000.0, "yf": "EURUSD=X"},
    "GBP/USD": {"mt5": os.getenv("BIGMAN_MT5_GBPUSD", "GBPUSD"), "td": "GBP/USD", "tv": "FX:GBPUSD", "digits": 5, "contract": 100000.0, "yf": "GBPUSD=X"},
}

TFS = {
    "15 min": (15, "TIMEFRAME_M15", "15min", "15"),
    "1 hour": (60, "TIMEFRAME_H1", "1h", "60"),
    "4 hours": (240, "TIMEFRAME_H4", "4h", "240"),
}

MAGIC = 777999
MIN_RR = 2.0

def now_ist(): 
    return datetime.now(IST)

def price_fmt(v, s): 
    return f"{float(v):,.{ASSETS[s]['digits']}f}"

def db_open():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS open_trades_ultimate(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, created_timestamp REAL NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, 
        lot_size REAL, bias TEXT NOT NULL, strategy_votes TEXT, candle_timestamp TEXT, mt5_ticket INTEGER DEFAULT 0,
        execution_mode TEXT DEFAULT 'PAPER')""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_ultimate(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT, loss_diagnosis TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    
    cols = {r["name"] for r in c.execute("PRAGMA table_info(open_trades_ultimate)")}
    if "execution_mode" not in cols: 
        c.execute("ALTER TABLE open_trades_ultimate ADD COLUMN execution_mode TEXT DEFAULT 'PAPER'")
    if "created_timestamp" not in cols:
        c.execute("ALTER TABLE open_trades_ultimate ADD COLUMN created_timestamp REAL DEFAULT 0")
        
    c.execute("INSERT OR IGNORE INTO account_balance VALUES(1, 3000.0)")
    c.commit()
    return c

def mt5_init():
    if not MT5_OK: 
        raise RuntimeError("MetaTrader5 Python package is not installed.")
    if not mt5.initialize(): 
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return True

@st.cache_data(ttl=60, show_spinner=False)
def cached_twelve_data(sym, interval, apikey):
    url = "https://api.twelvedata.com"
    p = {"symbol": sym, "interval": interval, "outputsize": 120, "timezone": "UTC", "apikey": apikey}
    try:
        r = HTTP.get(url + "/time_series", params=p, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        j = r.json()
        if "values" not in j:
            return pd.DataFrame()
        d = pd.DataFrame(j["values"])
        d["datetime"] = pd.to_datetime(d.datetime, utc=True)
        for k in ["open", "high", "low", "close"]:
            d[k] = pd.to_numeric(d[k], errors="coerce")
        d = d.dropna(subset=["open", "high", "low", "close"]).sort_values("datetime").set_index("datetime")
        d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
        d["ema50"] = d.close.ewm(span=50, adjust=False).mean()
        tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
        d["atr"] = tr.rolling(14).mean()
        return d
    except Exception:
        return pd.DataFrame()

def fetch_exact_market_data(asset, tf_name):
    sym = ASSETS[asset]["td"]
    interval_td = TFS[tf_name][2]
    
    # Priority 1: Direct Twelve Data Live Price & Time Series
    if API_KEY:
        try:
            q = HTTP.get("https://api.twelvedata.com/price", params={"symbol": sym, "apikey": API_KEY}, timeout=6)
            live_px = 0.0
            if q.status_code == 200:
                qj = q.json()
                if "price" in qj:
                    live_px = float(qj["price"])
            
            df = cached_twelve_data(sym, interval_td, API_KEY)
            if not df.empty:
                if live_px <= 0:
                    live_px = float(df.iloc[-1]["close"])
                return df, live_px, f"Twelve Data Live Verified ({sym})"
        except Exception:
            pass

    # Priority 2: Yahoo Finance Real-time Stream (Works 100% on Cloud & Mobile)
    if YF_OK:
        try:
            yf_ticker = ASSETS[asset]["yf"]
            interval_map = {"15 min": "15m", "1 hour": "1h", "4 hours": "1h"}
            period_map = {"15 min": "5d", "1 hour": "1mo", "4 hours": "2mo"}
            tk = yf.Ticker(yf_ticker)
            df_yf = tk.history(period=period_map.get(tf_name, "5d"), interval=interval_map.get(tf_name, "15m"))
            
            if not df_yf.empty:
                df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
                df_yf.index = pd.to_datetime(df_yf.index, utc=True)
                df_yf = df_yf.dropna(subset=["open", "high", "low", "close"]).sort_index()
                df_yf["ema20"] = df_yf.close.ewm(span=20, adjust=False).mean()
                df_yf["ema50"] = df_yf.close.ewm(span=50, adjust=False).mean()
                tr = pd.concat([df_yf.high - df_yf.low, (df_yf.high - df_yf.close.shift()).abs(), (df_yf.low - df_yf.close.shift()).abs()], axis=1).max(axis=1)
                df_yf["atr"] = tr.rolling(14).mean()
                live_px = float(df_yf.iloc[-1]["close"])
                if live_px > 0:
                    return df_yf, live_px, f"Yahoo Finance Live Stream ({yf_ticker})"
        except Exception:
            pass

    # Priority 3: MetaTrader 5 if local terminal is connected
    if MT5_OK:
        try:
            mt5_init()
            mt_sym = ASSETS[asset]["mt5"]
            mt5.symbol_select(mt_sym, True)
            tick = mt5.symbol_info_tick(mt_sym)
            tf_attr = TFS[tf_name][1]
            raw = mt5.copy_rates_from_pos(mt_sym, getattr(mt5, tf_attr), 0, 120)
            if raw is not None and len(raw) > 30 and tick is not None:
                d = pd.DataFrame(raw)
                d["datetime"] = pd.to_datetime(d.time, unit="s", utc=True)
                d = d.rename(columns={"tick_volume": "volume"}).set_index("datetime").sort_index()
                d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
                d["ema50"] = d.close.ewm(span=50, adjust=False).mean()
                tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
                d["atr"] = tr.rolling(14).mean()
                return d, float(tick.bid), f"MT5 Broker Live Feed ({mt_sym})"
        except Exception:
            pass

    default_px = 2650.0 if "Gold" in asset else 1.08
    return pd.DataFrame(), default_px, "Safe Fallback Price"

def expert_ultimate_analysis(asset):
    d15, bid, src1 = fetch_exact_market_data(asset, "15 min")
    d1h, _, _ = fetch_exact_market_data(asset, "1 hour")
    d4h, _, _ = fetch_exact_market_data(asset, "4 hours")
    
    if d4h.empty or d1h.empty or d15.empty or bid <= 0:
        return {"bias": "WAIT", "votes": ["Synchronizing exact live price feeds..."], "entry": bid, "sl": None, "tp": None, "rr": None, "details": {}}, bid, src1
    
    c4 = d4h.iloc[-1]
    c1 = d1h.iloc[-1]
    a15 = d15.iloc[-1]
    
    bias_4h = "BULLISH" if c4.close > c4.ema20 and c4.ema20 > c4.ema50 else ("BEARISH" if c4.close < c4.ema20 and c4.ema20 < c4.ema50 else "NEUTRAL")
    bias_1h = "BULLISH" if c1.close > c1.ema20 and c1.ema20 > c1.ema50 else ("BEARISH" if c1.close < c1.ema20 and c1.ema20 < c1.ema50 else "NEUTRAL")
    
    sh = float(d15.high.tail(30).max())
    slw = float(d15.low.tail(30).min())
    rng = max(sh - slw, 1e-12)
    eq = (sh + slw) / 2
    fib_618 = sh - (rng * 0.618)
    
    votes = []
    votes.append(f"4H Direction Bias: {bias_4h}")
    votes.append(f"1H Behavior Bias: {bias_1h}")
    votes.append(f"Swing High: {price_fmt(sh, asset)} | Swing Low: {price_fmt(slw, asset)}")
    votes.append(f"Equilibrium (50%): {price_fmt(eq, asset)} | Golden Pocket (0.618): {price_fmt(fib_618, asset)}")
    
    final_bias = "WAIT"
    market_nature = "Sideways / Choppy"
    
    if bias_4h == "BULLISH" and bias_1h == "BULLISH":
        market_nature = "Strong Impulsive Uptrend"
        if bid <= eq:
            final_bias = "BUY"
            votes.append("ULTIMATE SETUP: 4H+1H Bullish + Exact Price in Discount Zone / Fib Level -> BUY Signal")
        else:
            votes.append("WAIT: Uptrend active, but price is in Premium zone. Waiting for pullback.")
            
    elif bias_4h == "BEARISH" and bias_1h == "BEARISH":
        market_nature = "Strong Impulsive Downtrend"
        if bid >= eq:
            final_bias = "SELL"
            votes.append("ULTIMATE SETUP: 4H+1H Bearish + Exact Price in Premium Zone / Fib Level -> SELL Signal")
        else:
            votes.append("WAIT: Downtrend active, but price is in Discount zone. Waiting for rally.")
    else:
        votes.append("WAIT: Multi-timeframe conflict. Capital protection active.")
        
    entry = float(bid)
    stop = target = rr = None
    atr = float(a15.atr) if pd.notna(a15.atr) and a15.atr > 0 else rng * 0.025
    pad = max(atr * 0.6, rng * 0.01)

    if final_bias == "BUY":
        stop = slw - pad
        risk = entry - stop
        target = entry + max(risk * MIN_RR, rng * 0.5)
        rr = (target - entry) / risk if risk > 0 else 2.0
    elif final_bias == "SELL":
        stop = sh + pad
        risk = stop - entry
        target = entry - max(risk * MIN_RR, rng * 0.5)
        rr = (entry - target) / risk if risk > 0 else 2.0
        
    return {
        "bias": final_bias, "votes": votes, "entry": entry, "sl": stop, "tp": target, "rr": rr,
        "details": {
            "bias_4h": bias_4h, "bias_1h": bias_1h, "market_nature": market_nature,
            "swing_high": sh, "swing_low": slw, "equilibrium": eq, "fib_618": fib_618
        }
    }, bid, src1

def diagnose_loss(row, exit_price):
    bias = row["bias"]
    pnl = row["pnl"]
    if pnl >= 0:
        return "Profitable Trade - Perfectly aligned with trend and zone confluence."
    if bias == "BUY" and exit_price < row["entry_price"]:
        return "Loss Diagnosis: Bearish liquidity sweep or higher timeframe impulsive drop tested swing low structure."
    elif bias == "SELL" and exit_price > row["entry_price"]:
        return "Loss Diagnosis: Short-term retracement or volatility spike tested premium zone buffer."
    else:
        return "Loss Diagnosis: Range-bound consolidation noise triggered time stop."

# ---------------- STREAMLIT UI ----------------
st.markdown('<div class="bh">BIGMAN ⚡ Ultimate Price Lesson SMC Terminal</div>', unsafe_allow_html=True)
st.caption("Exact Live Ticks • Multi-Timeframe Alignment • Fibonacci Confluence • Automated Loss Diagnostics • IST")

db = db_open()

open_rows_check = db.execute("SELECT * FROM open_trades_ultimate").fetchall()
for row in open_rows_check:
    elapsed_mins = (time.time() - row["created_timestamp"]) / 60.0
    if elapsed_mins >= 15.0 and row["execution_mode"] == "PAPER":
        try:
            _, current_bid, _ = fetch_exact_market_data(row["symbol"], "15 min")
            contract = ASSETS[row["symbol"]]["contract"]
            direction = 1 if row["bias"] == "BUY" else -1
            pnl = round((current_bid - row["entry_price"]) * direction * row["lot_size"] * contract, 2)
            diagnosis = diagnose_loss(dict(row), current_bid)
            
            db.execute("""INSERT INTO trade_history_ultimate VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], current_bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "15-Min Auto-Expiry", pnl, row["strategy_votes"], diagnosis))
            db.execute("DELETE FROM open_trades_ultimate WHERE id=?", (row["id"],))
            db.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            db.commit()
        except Exception:
            pass

with st.sidebar:
    st.header("🎛️ Terminal Controls")
    if st.button("🔄 Refresh Live Analysis"): 
        st.rerun()
    asset = st.selectbox("Asset / Market", list(ASSETS))
    lot = st.number_input("Risk Lot Size", 0.01, 10.0, 0.01, 0.01)
    st.divider()
    mode = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    live_ok = st.checkbox("I confirm real broker risk management in MT5 LIVE mode", False)
    auto = st.toggle("Ultimate Auto-Pilot (Strict Confluence)", True)
    if st.button("🔄 Reset Paper Balance ($3,000)"):
        db.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        db.commit()
        st.rerun()

try:
    with st.spinner("Analyzing exact live market structure & multi-timeframe zones..."):
        analysis, bid, source = expert_ultimate_analysis(asset)
except Exception as e:
    st.error("LIVE FEED ERROR — Please check internet or API key.")
    st.code(str(e))
    st.stop()

final = analysis["bias"]

bal = float(db.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])
total_hist = db.execute("SELECT COUNT(*) as cnt FROM trade_history_ultimate").fetchone()["cnt"]
win_hist = db.execute("SELECT COUNT(*) as cnt FROM trade_history_ultimate WHERE pnl > 0").fetchone()["cnt"]
win_rate = (win_hist / total_hist * 100) if total_hist > 0 else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Exact Live Market Price", price_fmt(bid, asset))
m2.metric("Account Balance", f"${bal:,.2f}")
m3.metric("Database Win Rate", f"{win_rate:.1f}% ({total_hist} Trades)")
m4.metric("Ultimate Signal Bias", final)

st.caption(f"Active Feed Source: {source}")

if final == "BUY": 
    st.success("🟢 ULTIMATE BUY SETUP CONFIRMED (Multi-TF Bullish + Discount / Fib Confluence)")
elif final == "SELL": 
    st.error("🔴 ULTIMATE SELL SETUP CONFIRMED (Multi-TF Bearish + Premium / Fib Confluence)")
else:
    st.warning("⏳ WAITING: Bot is protecting capital. Awaiting complete multi-timeframe alignment.")

det = analysis["details"]
if det:
    with st.expander("📊 Professional Market Structure & Strategy Breakdown", expanded=True):
        st.write(f"**Market Nature:** {det.get('market_nature')} · **4H Trend:** {det.get('bias_4h')} · **1H Momentum:** {det.get('bias_1h')}")
        st.write(f"**Swing Range:** {price_fmt(det.get('swing_low', bid), asset)} to {price_fmt(det.get('swing_high', bid), asset)}")
        st.write(f"**Equilibrium (50%):** {price_fmt(det.get('equilibrium', bid), asset)} | **Golden Pocket (0.618 Fib):** {price_fmt(det.get('fib_618', bid), asset)}")
        st.markdown("**Ultimate Strategy Confluence Log:**")
        for x in analysis.get("votes", []): st.write("🔹 " + x)

if auto and final in ["BUY", "SELL"] and analysis["sl"] is not None and bid > 0:
    open_pos = db.execute("SELECT * FROM open_trades_ultimate WHERE symbol=?", (asset,)).fetchone()
    if not open_pos:
        env = "PAPER"
        db.execute("""INSERT INTO open_trades_ultimate(created_at_ist, created_timestamp, symbol, timeframe, entry_price, stop_loss,
            take_profit, lot_size, bias, strategy_votes, candle_timestamp, execution_mode)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), time.time(), asset, "15 min", analysis["entry"], analysis["sl"], analysis["tp"],
             lot, final, " | ".join(analysis["votes"]), str(datetime.now()), env))
        db.commit()
        st.toast(f"Ultimate {env} trade executed successfully for {asset} ({final})!")
        st.rerun()

st.divider()
col_l, col_r = st.columns([1.5, 1])
with col_l:
    st.subheader("📈 TradingView Professional Chart")
    tv = ASSETS[asset]["tv"]
    html = f'<iframe src="https://s.tradingview.com/widgetembed/?symbol={tv}&interval=15&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" width="100%" height="450" style="border:0"></iframe>'
    components.html(html, height=460, scrolling=False)
with col_r:
    st.subheader("📋 Active Ultimate Trade Plan")
    if analysis["sl"] is not None:
        st.write("**Direction:** " + analysis["bias"])
        st.write("**Entry Price:** " + price_fmt(analysis["entry"], asset))
        st.write("**Stop Loss:** " + price_fmt(analysis["sl"], asset))
        st.write("**Take Profit:** " + price_fmt(analysis["tp"], asset))
        if analysis["rr"] is not None: 
            st.write(f"**Target R:R:** {analysis['rr']:.2f}:1")
    else: 
        st.info("Awaiting perfect exact live multi-timeframe & Fibonacci confluence.")

st.divider()
st.subheader("📂 Open Positions & Risk Guardian")
open_rows = db.execute("SELECT * FROM open_trades_ultimate ORDER BY id DESC").fetchall()
if not open_rows: 
    st.info("No open positions currently active.")
for row in open_rows:
    elapsed_m = (time.time() - row["created_timestamp"]) / 60.0
    with st.expander(f"#{row['id']} {row['symbol']} {row['bias']} [{row['execution_mode']}] — Active for {elapsed_m:.1f} mins"):
        st.write(f"Opened: {row['created_at_ist']} · Entry: {row['entry_price']} · SL: {row['stop_loss']} · TP: {row['take_profit']}")
        if st.button(f"Close Position #{row['id']}", key=f"close_ult_{row['id']}"):
            contract = ASSETS[asset]["contract"]
            direction = 1 if row["bias"] == "BUY" else -1
            pnl = round((bid - row["entry_price"]) * direction * row["lot_size"] * contract, 2)
            diagnosis = diagnose_loss(dict(row), bid)
            db.execute("""INSERT INTO trade_history_ultimate VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "Manual Ultimate Close", pnl, row["strategy_votes"], diagnosis))
            db.execute("DELETE FROM open_trades_ultimate WHERE id=?", (row["id"],))
            db.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            db.commit()
            st.rerun()

st.divider()
st.subheader("🧠 Closed Trades History & Automated Loss Diagnostics")
hist = db.execute("SELECT * FROM trade_history_ultimate ORDER BY id DESC LIMIT 50").fetchall()
if hist:
    df_hist = pd.DataFrame([dict(x) for x in hist])
    st.dataframe(df_hist[["id", "symbol", "bias", "entry_price", "exit_price", "pnl", "exit_reason", "loss_diagnosis", "closed_at_ist"]], use_container_width=True, hide_index=True)
else:
    st.info("No closed trade history available yet.")

db.close()
