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
HTTP.headers["User-Agent"] = "BIGMAN-PriceLesson/1.0"

st.set_page_config(
    page_title="BIGMAN | Price Lesson SMC Terminal Pro", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""<style>
.stApp { background: #0b0f19; color: #f3f4f6; font-family: 'Inter', sans-serif; }
h1, h2, h3 { color: white; font-family: 'Inter', sans-serif; }
.bh { font-size: 2.2rem; font-weight: 850; background: linear-gradient(90deg, #60a5fa, #34d399);
-webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.stButton>button { width: 100%; font-weight: 650; border-radius: 8px; background-color: #2563eb; color: white; border: none; }
.stButton>button:hover { background-color: #1d4ed8; }
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

MAGIC = 999888
PIVOT = 3
MIN_RR = 2.0

def now_ist(): 
    return datetime.now(IST)

def price_fmt(v, s): 
    return f"{float(v):,.{ASSETS[s]['digits']}f}"

def db_open():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS open_trades_v72(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, created_timestamp REAL NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, 
        lot_size REAL, bias TEXT NOT NULL, strategy_votes TEXT, candle_timestamp TEXT, mt5_ticket INTEGER DEFAULT 0,
        execution_mode TEXT DEFAULT 'PAPER')""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_v72(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    
    cols = {r["name"] for r in c.execute("PRAGMA table_info(open_trades_v72)")}
    if "execution_mode" not in cols: 
        c.execute("ALTER TABLE open_trades_v72 ADD COLUMN execution_mode TEXT DEFAULT 'PAPER'")
    if "created_timestamp" not in cols:
        c.execute("ALTER TABLE open_trades_v72 ADD COLUMN created_timestamp REAL DEFAULT 0")
        
    c.execute("INSERT OR IGNORE INTO account_balance VALUES(1, 3000.0)")
    c.commit()
    return c

def fetch_tf_data(asset, interval_td, n=100):
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

def analyze_multi_timeframe(asset, quote):
    # Fetch Multi-Timeframe Data: 4H (Direction), 1H (Behavior), 15M (Entry)
    d4h = fetch_tf_data(asset, "4h", 50)
    d1h = fetch_tf_data(asset, "1h", 50)
    d15 = fetch_tf_data(asset, "15min", 100)
    
    if d4h.empty or d1h.empty or d15.empty:
        return {"bias": "WAIT", "votes": ["Fetching multi-timeframe data..."], "entry": quote, "sl": None, "tp": None, "rr": None, "details": {}}
    
    c4 = d4h.iloc[-1]
    c1 = d1h.iloc[-1]
    a15 = d15.iloc[-1]
    
    # Trend Determination per timeframe
    bias_4h = "BULLISH" if c4.close > c4.ema20 and c4.ema20 > c4.ema50 else ("BEARISH" if c4.close < c4.ema20 and c4.ema20 < c4.ema50 else "NEUTRAL")
    bias_1h = "BULLISH" if c1.close > c1.ema20 and c1.ema20 > c1.ema50 else ("BEARISH" if c1.close < c1.ema20 and c1.ema20 < c1.ema50 else "NEUTRAL")
    
    # 15M Swing & S7 Equilibrium
    sh = float(d15.high.tail(20).max())
    slw = float(d15.low.tail(20).min())
    eq = (sh + slw) / 2
    span = max(sh - slw, 1e-12)
    premium = quote >= eq
    
    votes = []
    votes.append(f"4H Direction Bias: {bias_4h} (EMA20 vs EMA50)")
    votes.append(f"1H Behavior Bias: {bias_1h} (Momentum Check)")
    votes.append(f"15M Zone: {'PREMIUM (Sell zone)' if premium else 'DISCOUNT (Buy zone)'}")
    
    # STRICT MULTI-TIMEFRAME CONFLUENCE RULE
    # Only execute if 4H and 1H match completely!
    final_bias = "WAIT"
    if bias_4h == "BULLISH" and bias_1h == "BULLISH" and not premium:
        final_bias = "BUY"
        votes.append("PERFECT CONFLUENCE: 4H + 1H Bullish & 15M Discount Zone -> BUY Execution")
    elif bias_4h == "BEARISH" and bias_1h == "BEARISH" and premium:
        final_bias = "SELL"
        votes.append("PERFECT CONFLUENCE: 4H + 1H Bearish & 15M Premium Zone -> SELL Execution")
    else:
        votes.append("WAIT: Multi-timeframe biases do not match or waiting for valid zone confluence.")
        
    entry = float(quote)
    stop = target = rr = None
    atr = float(a15.atr) if pd.notna(a15.atr) and a15.atr > 0 else span * 0.03
    pad = max(atr * 0.7, span * 0.012) # Strong buffer to prevent noise hits

    if final_bias == "BUY":
        stop = slw - pad
        risk = entry - stop
        target = entry + max(risk * MIN_RR, span * 0.6)
        rr = (target - entry) / risk if risk > 0 else 2.0
    elif final_bias == "SELL":
        stop = sh + pad
        risk = stop - entry
        target = entry - max(risk * MIN_RR, span * 0.6)
        rr = (entry - target) / risk if risk > 0 else 2.0
        
    return {
        "bias": final_bias, "votes": votes, "entry": entry, "sl": stop, "tp": target, "rr": rr,
        "details": {
            "bias_4h": bias_4h, "bias_1h": bias_1h,
            "premium_discount": "PREMIUM" if premium else "DISCOUNT",
            "swing_high": sh, "swing_low": slw, "equilibrium": eq
        }
    }

def send_live_order(asset, bias, lot, sl, tp):
    mt5_init()
    sym = ASSETS[asset]["mt5"]
    if not mt5.symbol_select(sym, True): 
        return False, 0, "Cannot select " + sym
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    if info is None or tick is None: 
        return False, 0, "No broker tick/info"
    typ = mt5.ORDER_TYPE_BUY if bias == "BUY" else mt5.ORDER_TYPE_SELL
    px = float(tick.ask if bias == "BUY" else tick.bid)
    digits = int(info.digits)
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(lot), "type": typ,
        "price": px, "sl": round(float(sl), digits), "tp": round(float(tp), digits), "deviation": 30,
        "magic": MAGIC, "comment": "BIGMAN Multi-TF PriceLesson", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC
    }
    res = mt5.order_send(req)
    if res is None: 
        return False, 0, str(mt5.last_error())
    if res.retcode != mt5.TRADE_RETCODE_DONE: 
        return False, 0, f"Broker rejected: {res.retcode} {res.comment}"
    return True, int(res.order), f"MT5 order #{res.order} accepted"

def open_trade(c, asset, tf, r, lot, ts, mode, ticket=0):
    c.execute("""INSERT INTO open_trades_v72(created_at_ist, created_timestamp, symbol, timeframe, entry_price, stop_loss,
        take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket, execution_mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), time.time(), asset, tf, r["entry"], r["sl"], r["tp"],
         lot, r["bias"], " | ".join(r["votes"]), str(ts), ticket, mode))
    c.commit()

def paper_pnl(row, exit_price, asset):
    contract = ASSETS[asset]["contract"]
    direction = 1 if row["bias"] == "BUY" else -1
    return round((exit_price - row["entry_price"]) * direction * row["lot_size"] * contract, 2)

# ---------------- STREAMLIT UI ----------------
st.markdown('<div class="bh">BIGMAN ⚡ Multi-Timeframe Price Lesson Terminal Pro</div>', unsafe_allow_html=True)
st.caption("Strict Multi-Timeframe Confluence (4H Direction + 1H Behavior + 15M Entry) • IST")

c = db_open()

open_rows_check = c.execute("SELECT * FROM open_trades_v72").fetchall()
for row in open_rows_check:
    elapsed_mins = (time.time() - row["created_timestamp"]) / 60.0
    if elapsed_mins >= 15.0 and row["execution_mode"] == "PAPER":
        try:
            _, current_bid, _, _, _ = fetch_tf_data(row["symbol"], "15min", 10).iloc[-1].close, 0, 0, 0, 0
            # Fallback bid
            current_bid = row["entry_price"]
        except Exception:
            pass

with st.sidebar:
    st.header("🎛️ Terminal Controls")
    if st.button("🔄 Refresh Market Data"): 
        st.rerun()
    asset = st.selectbox("Asset", list(ASSETS))
    lot = st.number_input("Lot Size", 0.01, 10.0, 0.01, 0.01)
    st.divider()
    mode = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    live_ok = st.checkbox("I confirm real broker orders in MT5 LIVE mode", False)
    auto = st.toggle("Auto-pilot (Strict Multi-TF Confluence)", True)
    if st.button("🔄 Reset Paper Balance ($3,000)"):
        c.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        c.commit()
        st.rerun()

try:
    with st.spinner("Analyzing Multi-Timeframe Confluence (4H -> 1H -> 15M)..."):
        d15_check = fetch_tf_data(asset, "15min", 10)
        bid = float(d15_check.iloc[-1]["close"]) if not d15_check.empty else 0.0
except Exception as e:
    st.error("LIVE FEED UNAVAILABLE — Please wait 30 seconds.")
    st.code(str(e))
    st.stop()

r_multi = analyze_multi_timeframe(asset, bid)
final = r_multi["bias"]

bal = float(c.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])
m1, m2, m3, m4 = st.columns(4)
m1.metric("Live Price Reference", price_fmt(bid, asset))
m2.metric("Paper Balance", f"${bal:,.2f}")
m3.metric("Multi-TF Status", "ALIGNED" if final != "WAIT" else "WAITING FOR CONFLUENCE")
m4.metric("Active Signal", final)

if final == "BUY": 
    st.success("🟢 PERFECT BUY CONFLUENCE (4H + 1H Bullish & Discount Zone)")
elif final == "SELL": 
    st.error("🔴 PERFECT SELL CONFLUENCE (4H + 1H Bearish & Premium Zone)")
else:
    st.warning("⏳ WAITING: Multi-timeframe biases are mixed. Bot will NOT take any low-probability trades.")

det = r_multi["details"]
with st.expander("📊 Multi-Timeframe Strategy Breakdown", expanded=True):
    st.write(f"**4H Direction Bias:** {det.get('bias_4h')} · **1H Behavior Bias:** {det.get('bias_1h')} · **15M Zone:** {det.get('premium_discount')}")
    st.markdown("**Confluence Log & Rules:**")
    for x in r_multi.get("votes", []): st.write("🔹 " + x)

if auto and final in ["BUY", "SELL"] and r_multi["sl"] is not None:
    open_pos = c.execute("SELECT * FROM open_trades_v72 WHERE symbol=?", (asset,)).fetchone()
    if not open_pos:
        ticket = 0
        env = "PAPER"
        if mode == "MT5 LIVE BROKER" and live_ok:
            ok, ticket, msg = send_live_order(asset, final, lot, r_multi["sl"], r_multi["tp"])
            if ok: env = "LIVE"
        open_trade(c, asset, "15 min", r_multi, lot, str(datetime.now()), env, ticket)
        st.toast(f"Multi-TF Aligned {env} trade executed for {asset} ({final})!")
        st.rerun()

st.divider()
st.subheader("📋 Active Trade Plan")
if r_multi["sl"] is not None:
    st.write("**Direction:** " + r_multi["bias"])
    st.write("**Entry Reference:** " + price_fmt(r_multi["entry"], asset))
    st.write("**Stop Loss:** " + price_fmt(r_multi["sl"], asset))
    st.write("**Take Profit:** " + price_fmt(r_multi["tp"], asset))
    if r_multi["rr"] is not None: 
        st.write(f"**Target R:R:** {r_multi['rr']:.2f}:1")
else: 
    st.info("Awaiting 4H + 1H + 15M complete alignment. No forced trades.")

st.divider()
st.subheader("📜 Closed Trades History")
hist = c.execute("SELECT * FROM trade_history_v72 ORDER BY id DESC LIMIT 50").fetchall()
if hist:
    h = pd.DataFrame([dict(x) for x in hist])
    st.dataframe(h[["id", "symbol", "bias", "entry_price", "exit_price", "pnl", "exit_reason", "closed_at_ist"]], use_container_width=True, hide_index=True)
else:
    st.info("No closed trades yet.")

c.close()
