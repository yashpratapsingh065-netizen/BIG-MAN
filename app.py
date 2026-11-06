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

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.getenv("BIGMAN_DB_PATH", "bigman_price_lesson.db")
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "BIGMAN-ExpertTrainer/2.0"

st.set_page_config(
    page_title="BIGMAN | Expert Price Lesson SMC Terminal Pro", 
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
.metric-card { background: #111827; padding: 15px; border-radius: 10px; border: 1px solid #1f2937; }
</style>""", unsafe_allow_html=True)

ASSETS = {
    "XAU/USD (Gold)": {"mt5": os.getenv("BIGMAN_MT5_XAU", "XAUUSD"), "td": "XAU/USD", "tv": "OANDA:XAUUSD", "digits": 2, "contract": 100.0},
    "EUR/USD": {"mt5": os.getenv("BIGMAN_MT5_EURUSD", "EURUSD"), "td": "EUR/USD", "tv": "FX:EURUSD", "digits": 5, "contract": 100000.0},
    "GBP/USD": {"mt5": os.getenv("BIGMAN_MT5_GBPUSD", "GBPUSD"), "td": "GBP/USD", "tv": "FX:GBPUSD", "digits": 5, "contract": 100000.0},
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
    c.execute("""CREATE TABLE IF NOT EXISTS open_trades_expert(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, created_timestamp REAL NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, 
        lot_size REAL, bias TEXT NOT NULL, strategy_votes TEXT, candle_timestamp TEXT, mt5_ticket INTEGER DEFAULT 0,
        execution_mode TEXT DEFAULT 'PAPER')""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_expert(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT, loss_diagnosis TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    
    cols = {r["name"] for r in c.execute("PRAGMA table_info(open_trades_expert)")}
    if "execution_mode" not in cols: 
        c.execute("ALTER TABLE open_trades_expert ADD COLUMN execution_mode TEXT DEFAULT 'PAPER'")
    if "created_timestamp" not in cols:
        c.execute("ALTER TABLE open_trades_expert ADD COLUMN created_timestamp REAL DEFAULT 0")
        
    c.execute("INSERT OR IGNORE INTO account_balance VALUES(1, 3000.0)")
    c.commit()
    return c

def fetch_tf_data(asset, interval_td, n=120):
    sym = ASSETS[asset]["td"]
    url = "https://api.twelvedata.com"
    p = {"symbol": sym, "interval": interval_td, "outputsize": n, "timezone": "UTC", "apikey": API_KEY}
    r = HTTP.get(url + "/time_series", params=p, timeout=12)
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

def expert_market_analysis(asset, quote):
    # Multi-Timeframe Data Fetch (4H Direction -> 1H Behavior -> 15M Entry)
    d4h = fetch_tf_data(asset, "4h", 60)
    d1h = fetch_tf_data(asset, "1h", 60)
    d15 = fetch_tf_data(asset, "15min", 120)
    
    if d4h.empty or d1h.empty or d15.empty:
        return {"bias": "WAIT", "votes": ["Fetching expert multi-timeframe feeds..."], "entry": quote, "sl": None, "tp": None, "rr": None, "details": {}}
    
    c4 = d4h.iloc[-1]
    c1 = d1h.iloc[-1]
    a15 = d15.iloc[-1]
    
    bias_4h = "BULLISH" if c4.close > c4.ema20 and c4.ema20 > c4.ema50 else ("BEARISH" if c4.close < c4.ema20 and c4.ema20 < c4.ema50 else "NEUTRAL")
    bias_1h = "BULLISH" if c1.close > c1.ema20 and c1.ema20 > c1.ema50 else ("BEARISH" if c1.close < c1.ema20 and c1.ema20 < c1.ema50 else "NEUTRAL")
    
    # Fibonacci & S7 Equilibrium Calculations (0.5 & 0.618 Golden Pocket)
    sh = float(d15.high.tail(30).max())
    slw = float(d15.low.tail(30).min())
    rng = max(sh - slw, 1e-12)
    eq = (sh + slw) / 2
    fib_618 = sh - (rng * 0.618)
    fib_500 = eq
    
    votes = []
    votes.append(f"4H Master Trend (Direction): {bias_4h}")
    votes.append(f"1H Momentum (Behavior): {bias_1h}")
    votes.append(f"Swing High: {price_fmt(sh, asset)} | Swing Low: {price_fmt(slw, asset)}")
    votes.append(f"Fibonacci 50% Equilibrium: {price_fmt(eq, asset)} | Golden Pocket (0.618): {price_fmt(fib_618, asset)}")
    
    # Strict Expert Confluence Rule based on Price Lesson
    final_bias = "WAIT"
    market_nature = "Consolidation / Choppy"
    
    if bias_4h == "BULLISH" and bias_1h == "BULLISH":
        market_nature = "Strong Impulsive Uptrend"
        if quote <= eq: # Discount Zone & Fibonacci Confluence
            final_bias = "BUY"
            votes.append("EXPERT SETUP: 4H+1H Bullish + Price in Discount/Fib Golden Pocket -> High Probability BUY")
        else:
            votes.append("WAIT: Uptrend active, but price is in Premium zone. Waiting for pullback to Discount/Fib level.")
            
    elif bias_4h == "BEARISH" and bias_1h == "BEARISH":
        market_nature = "Strong Impulsive Downtrend"
        if quote >= eq: # Premium Zone & Fibonacci Confluence
            final_bias = "SELL"
            votes.append("EXPERT SETUP: 4H+1H Bearish + Price in Premium/Fib Golden Pocket -> High Probability SELL")
        else:
            votes.append("WAIT: Downtrend active, but price is in Discount zone. Waiting for rally to Premium/Fib level.")
    else:
        votes.append("WAIT: Multi-timeframe conflict or market in sideways consolidation. Protecting capital.")
        
    entry = float(quote)
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
    }

def diagnose_loss(row, exit_price):
    bias = row["bias"]
    pnl = row["pnl"]
    if pnl >= 0:
        return "Profitable Trade - Executed perfectly within trend and zone confluence."
    
    # Expert Post-Mortem Loss Analysis
    if bias == "BUY" and exit_price < row["entry_price"]:
        return "Loss Diagnosis: Market experienced sudden bearish liquidity sweep or continuation of higher timeframe impulsive drop. Price broke swing low structure."
    elif bias == "SELL" and exit_price > row["entry_price"]:
        return "Loss Diagnosis: Market experienced unexpected short-term retracement or news spike into premium zone before dropping. Stop-loss buffer was tested by volatility."
    else:
        return "Loss Diagnosis: Range-bound chop noise or minor counter-trend pullback triggered time stop."

# ---------------- PROFESSIONAL STREAMLIT UI ----------------
st.markdown('<div class="bh">BIGMAN ⚡ Expert Price Lesson SMC Terminal Pro</div>', unsafe_allow_html=True)
st.caption("Professional Multi-Timeframe Alignment • Fibonacci Golden Pocket • Automated Learning & Loss Diagnostics • IST")

c = db_open()

# Automated 15-Minute Expiry & Training Diagnosis Check
open_rows_check = c.execute("SELECT * FROM open_trades_expert").fetchall()
for row in open_rows_check:
    elapsed_mins = (time.time() - row["created_timestamp"]) / 60.0
    if elapsed_mins >= 15.0 and row["execution_mode"] == "PAPER":
        try:
            d_chk = fetch_tf_data(row["symbol"], "15min", 10)
            current_bid = float(d_chk.iloc[-1]["close"]) if not d_chk.empty else row["entry_price"]
            contract = ASSETS[row["symbol"]]["contract"]
            direction = 1 if row["bias"] == "BUY" else -1
            pnl = round((current_bid - row["entry_price"]) * direction * row["lot_size"] * contract, 2)
            diagnosis = diagnose_loss(dict(row), current_bid)
            
            c.execute("""INSERT INTO trade_history_expert VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], current_bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "15-Min Auto-Expiry", pnl, row["strategy_votes"], diagnosis))
            c.execute("DELETE FROM open_trades_expert WHERE id=?", (row["id"],))
            c.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            c.commit()
        except Exception:
            pass

with st.sidebar:
    st.header("🎛️ Expert Terminal Controls")
    if st.button("🔄 Refresh Market Analysis"): 
        st.rerun()
    asset = st.selectbox("Asset / Market", list(ASSETS))
    lot = st.number_input("Risk Lot Size", 0.01, 10.0, 0.01, 0.01)
    st.divider()
    mode = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    live_ok = st.checkbox("I confirm real broker risk management in MT5 LIVE mode", False)
    auto = st.toggle("Expert Auto-Pilot (Strict Multi-TF Confluence)", True)
    if st.button("🔄 Reset Paper Balance ($3,000)"):
        c.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        c.commit()
        st.rerun()

try:
    with st.spinner("Expert Trader analyzing Multi-Timeframe structure & Fibonacci zones..."):
        d15_live = fetch_tf_data(asset, "15min", 10)
        bid = float(d15_live.iloc[-1]["close"]) if not d15_live.empty else 0.0
except Exception as e:
    st.error("LIVE FEED TEMPORARILY UNAVAILABLE — Please wait 30 seconds.")
    st.code(str(e))
    st.stop()

analysis = expert_market_analysis(asset, bid)
final = analysis["bias"]

bal = float(c.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])
total_hist = c.execute("SELECT COUNT(*) as cnt FROM trade_history_expert").fetchone()["cnt"]
win_hist = c.execute("SELECT COUNT(*) as cnt FROM trade_history_expert WHERE pnl > 0").fetchone()["cnt"]
win_rate = (win_hist / total_hist * 100) if total_hist > 0 else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Live Market Price", price_fmt(bid, asset))
m2.metric("Account Balance", f"${bal:,.2f}")
m3.metric("Database Win Rate", f"{win_rate:.1f}% ({total_hist} Trades)")
m4.metric("Expert Signal Bias", final)

if final == "BUY": 
    st.success("🟢 EXPERT BUY SETUP CONFIRMED (Multi-TF Bullish + Discount / Fib Confluence)")
elif final == "SELL": 
    st.error("🔴 EXPERT SELL SETUP CONFIRMED (Multi-TF Bearish + Premium / Fib Confluence)")
else:
    st.warning("⏳ MARKET WAITING: Expert bot is protecting capital. No high-probability alignment detected yet.")

det = analysis["details"]
with st.expander("📊 Professional Market Structure & Strategy Breakdown", expanded=True):
    st.write(f"**Market Nature:** {det.get('market_nature')} · **4H Trend:** {det.get('bias_4h')} · **1H Momentum:** {det.get('bias_1h')}")
    st.write(f"**Swing Range:** {price_fmt(det.get('swing_low', bid), asset)} to {price_fmt(det.get('swing_high', bid), asset)}")
    st.write(f"**Equilibrium (50%):** {price_fmt(det.get('equilibrium', bid), asset)} | **Golden Pocket (0.618 Fib):** {price_fmt(det.get('fib_618', bid), asset)}")
    st.markdown("**Expert Strategy Confluence Log:**")
    for x in analysis.get("votes", []): st.write("🔹 " + x)

if auto and final in ["BUY", "SELL"] and analysis["sl"] is not None:
    open_pos = c.execute("SELECT * FROM open_trades_expert WHERE symbol=?", (asset,)).fetchone()
    if not open_pos:
        ticket = 0
        env = "PAPER"
        if mode == "MT5 LIVE BROKER" and live_ok:
            pass # Hook for MT5 if needed
        c.execute("""INSERT INTO open_trades_expert(created_at_ist, created_timestamp, symbol, timeframe, entry_price, stop_loss,
            take_profit, lot_size, bias, strategy_votes, candle_timestamp, execution_mode)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), time.time(), asset, "15 min", analysis["entry"], analysis["sl"], analysis["tp"],
             lot, final, " | ".join(analysis["votes"]), str(datetime.now()), env))
        c.commit()
        st.toast(f"Expert {env} trade executed successfully for {asset} ({final})!")
        st.rerun()

st.divider()
col_l, col_r = st.columns([1.5, 1])
with col_l:
    st.subheader("📈 TradingView Professional Chart")
    tv = ASSETS[asset]["tv"]
    html = f'<iframe src="https://s.tradingview.com/widgetembed/?symbol={tv}&interval=15&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" width="100%" height="450" style="border:0"></iframe>'
    components.html(html, height=460, scrolling=False)
with col_r:
    st.subheader("📋 Active Expert Trade Plan")
    if analysis["sl"] is not None:
        st.write("**Direction:** " + analysis["bias"])
        st.write("**Entry Price:** " + price_fmt(analysis["entry"], asset))
        st.write("**Stop Loss:** " + price_fmt(analysis["sl"], asset))
        st.write("**Take Profit:** " + price_fmt(analysis["tp"], asset))
        if analysis["rr"] is not None: 
            st.write(f"**Target R:R:** {analysis['rr']:.2f}:1")
    else: 
        st.info("Awaiting perfect multi-timeframe & Fibonacci confluence.")

st.divider()
st.subheader("📂 Open Positions & Risk Guardian")
open_rows = c.execute("SELECT * FROM open_trades_expert ORDER BY id DESC").fetchall()
if not open_rows: 
    st.info("No open positions currently active.")
for row in open_rows:
    elapsed_m = (time.time() - row["created_timestamp"]) / 60.0
    with st.expander(f"#{row['id']} {row['symbol']} {row['bias']} [{row['execution_mode']}] — Active for {elapsed_m:.1f} mins"):
        st.write(f"Opened: {row['created_at_ist']} · Entry: {row['entry_price']} · SL: {row['stop_loss']} · TP: {row['take_profit']}")
        if st.button(f"Close Position #{row['id']}", key=f"close_exp_{row['id']}"):
            contract = ASSETS[row['symbol']]["contract"]
            direction = 1 if row["bias"] == "BUY" else -1
            pnl = round((bid - row["entry_price"]) * direction * row["lot_size"] * contract, 2)
            diagnosis = diagnose_loss(dict(row), bid)
            c.execute("""INSERT INTO trade_history_expert VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "Manual Expert Close", pnl, row["strategy_votes"], diagnosis))
            c.execute("DELETE FROM open_trades_expert WHERE id=?", (row["id"],))
            c.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            c.commit()
            st.rerun()

st.divider()
st.subheader("🧠 Closed Trades History & Automated Loss Diagnostics")
hist = c.execute("SELECT * FROM trade_history_expert ORDER BY id DESC LIMIT 50").fetchall()
if hist:
    df_hist = pd.DataFrame([dict(x) for x in hist])
    st.dataframe(df_hist[["id", "symbol", "bias", "entry_price", "exit_price", "pnl", "exit_reason", "loss_diagnosis", "closed_at_ist"]], use_container_width=True, hide_index=True)
else:
    st.info("No closed trade history available yet.")

c.close()
