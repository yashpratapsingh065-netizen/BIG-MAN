import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.getenv("BIGMAN_DB_PATH", "bigman_price_lesson.db")

st.set_page_config(
    page_title="BIGMAN | Master Override Terminal", 
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
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "digits": 2, "contract": 100.0, "default": 3033.0},
    "EUR/USD": {"tv": "FX:EURUSD", "digits": 5, "contract": 100000.0, "default": 1.0850},
    "GBP/USD": {"tv": "FX:GBPUSD", "digits": 5, "contract": 100000.0, "default": 1.3000},
}

def now_ist(): return datetime.now(IST)
def price_fmt(v, s): return f"{float(v):,.{ASSETS[s]['digits']}f}"

def db_open():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS open_trades_master(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, created_timestamp REAL NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, 
        lot_size REAL, bias TEXT NOT NULL, strategy_votes TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_master(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    c.execute("INSERT OR IGNORE INTO account_balance VALUES(1, 3000.0)")
    c.commit()
    return c

def analyze_market(asset, bid):
    rng = bid * 0.0025
    sh = bid + (rng * 0.5)
    slw = bid - (rng * 0.5)
    eq = (sh + slw) / 2
    
    bias = "BUY" if bid <= eq else "SELL"
    votes = [
        f"Master Locked Chart Price: {price_fmt(bid, asset)}",
        f"Structure Swing High: {price_fmt(sh, asset)} | Low: {price_fmt(slw, asset)}",
        f"Equilibrium (50% Zone): {price_fmt(eq, asset)}",
        f"SMC Strategy Bias: {bias}"
    ]
    
    stop = slw - (rng * 0.2) if bias == "BUY" else sh + (rng * 0.2)
    target = bid + (rng * 1.2) if bias == "BUY" else bid - (rng * 1.2)
    risk = abs(bid - stop)
    rr = abs(target - bid) / risk if risk > 0 else 2.0
    
    return {"bias": bias, "votes": votes, "entry": bid, "sl": stop, "tp": target, "rr": rr}

# ---------------- UI ----------------
st.markdown('<div class="bh">BIGMAN ⚡ Master Override Terminal</div>', unsafe_allow_html=True)
st.caption("100% Exact Chart Match via Master Lock • Zero API Mismatch • IST")

db = db_open()

if "master_prices" not in st.session_state:
    st.session_state["master_prices"] = {}

with st.sidebar:
    st.header("🎛️ Terminal Controls")
    asset = st.selectbox("Asset", list(ASSETS))
    lot = st.number_input("Lot Size", 0.01, 10.0, 0.01, 0.01)
    auto = st.toggle("Auto-Pilot Execution", True)
    
    st.divider()
    if st.button("🔄 Reset Balance ($3,000)"):
        db.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        db.commit()
        st.rerun()
    if st.button("🗑️ Clear Open Positions"):
        db.execute("DELETE FROM open_trades_master")
        db.commit()
        st.rerun()

# MASTER OVERRIDE PANEL RIGHT ON TOP
st.markdown("### 🎯 Live Chart Price Master Lock")
col_inp1, col_inp2 = st.columns([3, 1])

default_val = st.session_state["master_prices"].get(asset, ASSETS[asset]["default"])

with col_inp1:
    user_entered_price = st.number_input(
        "Type exact price from your TradingView chart here:",
        value=float(default_val),
        format="%.2f" if "Gold" in asset else "%.5f",
        step=0.01 if "Gold" in asset else 0.0001,
        key=f"input_{asset}"
    )

with col_inp2:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔒 Lock Price"):
        st.session_state["master_prices"][asset] = user_entered_price
        st.success("Price Locked!")
        st.rerun()

bid = st.session_state["master_prices"].get(asset, user_entered_price)
analysis = analyze_market(asset, bid)
final = analysis["bias"]

bal = float(db.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])

m1, m2, m3 = st.columns(3)
m1.metric("Active Execution Price", price_fmt(bid, asset))
m2.metric("Account Balance", f"${bal:,.2f}")
m3.metric("Signal Bias", final)
st.caption("Status: Locked & Synced with your visual chart view")

if final == "BUY": st.success("🟢 MASTER BUY SETUP ACTIVE")
elif final == "SELL": st.error("🔴 MASTER SELL SETUP ACTIVE")

if auto and bid > 0 and final in ["BUY", "SELL"]:
    open_pos = db.execute("SELECT * FROM open_trades_master WHERE symbol=?", (asset,)).fetchone()
    if not open_pos:
        db.execute("INSERT INTO open_trades_master(created_at_ist, created_timestamp, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), time.time(), asset, "15 min", analysis["entry"], analysis["sl"], analysis["tp"], lot, final, " | ".join(analysis["votes"])))
        db.commit()
        st.toast(f"Trade executed at exact locked price {bid}!")
        st.rerun()

st.divider()
col_l, col_r = st.columns([1.5, 1])
with col_l:
    st.subheader("📈 TradingView Professional Chart")
    tv = ASSETS[asset]["tv"]
    components.html(f'<iframe src="https://s.tradingview.com/widgetembed/?symbol={tv}&interval=15&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" width="100%" height="450" style="border:0"></iframe>', height=460)
with col_r:
    st.subheader("📋 Active Trade Plan")
    if analysis["sl"] is not None:
        st.write(f"**Direction:** {analysis['bias']}")
        st.write(f"**Entry:** {price_fmt(analysis['entry'], asset)}")
        st.write(f"**SL:** {price_fmt(analysis['sl'], asset)}")
        st.write(f"**TP:** {price_fmt(analysis['tp'], asset)}")
        st.write(f"**Target R:R:** {analysis['rr']:.2f}:1")

st.divider()
st.subheader("📂 Open Positions")
open_rows = db.execute("SELECT * FROM open_trades_master ORDER BY id DESC").fetchall()
if not open_rows: st.info("No open positions active.")
for row in open_rows:
    with st.expander(f"#{row['id']} {row['symbol']} {row['bias']} — Active"):
        st.write(f"Entry: {row['entry_price']} · SL: {row['stop_loss']} · TP: {row['take_profit']}")
        if st.button(f"Close #{row['id']}", key=f"close_master_{row['id']}"):
            contract = ASSETS[asset]["contract"]
            pnl = round((bid - row["entry_price"]) * (1 if row["bias"] == "BUY" else -1) * row["lot_size"] * contract, 2)
            db.execute("INSERT INTO trade_history_master VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "Manual Close", pnl, row["strategy_votes"]))
            db.execute("DELETE FROM open_trades_master WHERE id=?", (row["id"],))
            db.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            db.commit()
            st.rerun()

db.close()
