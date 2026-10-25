import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BIG MAN — XAUUSD Gold Trading Bot",
    page_icon="🪙",
    layout="wide"
)

DB_PATH = os.getenv("BIGMAN_GOLD_DB_PATH", "bigman_gold_bot.db")

GOLD_SYMBOL = {"yf": "GC=F", "tv": "OANDA:XAUUSD", "lot": 1}

INTERVALS = {
    "5 min": {"yf": "5m", "minutes": 5, "tv": "5"},
    "15 min": {"yf": "15m", "minutes": 15, "tv": "15"},
    "1 hour": {"yf": "60m", "minutes": 60, "tv": "60"},
    "1 day": {"yf": "1d", "minutes": 1440, "tv": "D"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold_open_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            quantity INTEGER DEFAULT 1,
            bias TEXT NOT NULL,
            mode TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold_trade_history (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            closed_at TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            quantity INTEGER,
            bias TEXT NOT NULL,
            mode TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold_account_balance (
            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM gold_account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO gold_account_balance (id, balance) VALUES (1, 100.0)")
    con.commit()
    return con

def fetch_gold_market_data(timeframe_label):
    cfg = INTERVALS[timeframe_label]
    try:
        df = yf.download(GOLD_SYMBOL["yf"], period="5d", interval=cfg["yf"], progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [col[0].lower() for col in df.columns]
            else:
                df.columns = [col.lower() for col in df.columns]
            return df[["open", "high", "low", "close"]].tail(200), "XAUUSD Live Feed"
    except Exception:
        pass
    return pd.DataFrame(), "No Data"

def evaluate_gold_strategy(df):
    if len(df) < 30:
        return {"bias": "WAIT", "votes": "Insufficient Data", "bullish": 0, "bearish": 0, "entry": 0, "sl": 0, "tp": 0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df.high.tail(50).max())
    swing_low = float(df.low.tail(50).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    if last.close < eq_50:
        bullish_votes.append("Gold Discount Zone")
    else:
        bearish_votes.append("Gold Premium Zone")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Rejection")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score:
        bias = "BUY"
    elif s_score > b_score:
        bias = "SELL"
    else:
        bias = "WAIT"

    curr_close = float(last.close)
    sl_pts = max(rng * 1.5, 2.0)
    tp_pts = max(rng * 3.0, 4.0)

    sl = curr_close - sl_pts if bias == "BUY" else curr_close + sl_pts
    tp = curr_close + tp_pts if bias == "BUY" else curr_close - tp_pts

    votes_text = f"Bullish [{b_score}]: {', '.join(bullish_votes)} | Bearish [{s_score}]: {', '.join(bearish_votes)}"

    return {
        "bias": bias,
        "votes": votes_text,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(curr_close, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2)
    }

def get_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM gold_account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 100.0

def update_balance(con, delta):
    new_bal = get_balance(con) + delta
    con.execute("UPDATE gold_account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_trade(con, timeframe, result, qty, mode, candle_ts):
    if result["bias"] == "WAIT":
        return False

    cursor = con.cursor()
    cursor.execute("SELECT id FROM gold_open_trades WHERE timeframe = ? AND candle_timestamp = ?", (timeframe, str(candle_ts)))
    if cursor.fetchone():
        return False

    created_at = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p')
    con.execute(
        "INSERT INTO gold_open_trades (created_at, timeframe, entry_price, stop_loss, take_profit, quantity, bias, mode, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at, timeframe, result["entry"], result["sl"], result["tp"], qty, result["bias"], mode, result["votes"], str(candle_ts))
    )
    con.commit()
    return True

def close_trade(con, trade_id, current_price, reason="Manual Exit"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at, timeframe, entry_price, stop_loss, take_profit, quantity, bias, mode, strategy_votes FROM gold_open_trades WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0

    created_at, timeframe, entry_p, sl, tp, qty, bias, mode, votes = row
    diff = (current_price - entry_p) if bias == "BUY" else (entry_p - current_price)
    pnl = round(diff * qty, 2)
    closed_at = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p')

    con.execute(
        "INSERT INTO gold_trade_history (id, created_at, closed_at, timeframe, entry_price, exit_price, stop_loss, take_profit, quantity, bias, mode, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at, closed_at, timeframe, entry_p, round(current_price, 2), sl, tp, qty, bias, mode, reason, pnl, votes)
    )
    con.execute("DELETE FROM gold_open_trades WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl)
    return pnl

st.title("🪙 BIG MAN — XAUUSD Gold Trading Bot")
st.caption("Precious Metals SMC & Price Action Engine")

con = connect_db()

with st.sidebar:
    st.header("Control Panel")
    selected_tf = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    trade_qty = st.number_input("Quantity / Lots", min_value=1, max_value=1000, value=1, step=1)
    
    st.markdown("---")
    trading_mode = st.radio("Mode", ["PAPER TRADING", "LIVE (API)"], index=0)
    
    auto_pilot = st.toggle("Auto-Pilot Execution", value=False)
    
    if st.button("Execute Gold Trade", use_container_width=True):
        df_temp, _ = fetch_gold_market_data(selected_tf)
        if not df_temp.empty:
            res_temp = evaluate_gold_strategy(df_temp)
            candle_ts = df_temp.index[-1]
            mode_str = "LIVE" if "LIVE" in trading_mode else "PAPER"
            ok = execute_trade(con, selected_tf, res_temp, trade_qty, mode_str, candle_ts)
            if ok:
                st.sidebar.success("Gold trade executed!")
            else:
                st.sidebar.warning("Trade blocked: WAIT signal or already executed.")

    st.markdown("---")
    if st.button("Reset Balance to ₹100", use_container_width=True):
        con.execute("UPDATE gold_account_balance SET balance = 100.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset!")
        st.rerun()

df_live, feed_source = fetch_gold_market_data(selected_tf)

if not df_live.empty:
    strategy_res = evaluate_gold_strategy(df_live)
    live_p = float(df_live.close.iloc[-1])
    curr_ts = df_live.index[-1]
    mode_str = "LIVE" if "LIVE" in trading_mode else "PAPER"

    if auto_pilot:
        execute_trade(con, selected_tf, strategy_res, trade_qty, mode_str, curr_ts)

    curr_bal = get_balance(con)
    st.markdown(f"### Account Balance: **$ {curr_bal:,.2f} USD**")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Live Gold Price (XAUUSD)", f"${live_p:,.2f}")
    c2.metric("Signal Bias", strategy_res["bias"])
    c3.metric("Confluence Score", f"Bull: {strategy_res['bullish']} | Bear: {strategy_res['bearish']}")

st.divider()

col_chart, col_details = st.columns([1.8, 1])

with col_chart:
    st.subheader("Live XAUUSD Chart")
    tv_tf = INTERVALS[selected_tf]["tv"]
    
    tv_widget_html = f"""
    <div class="tradingview-widget-container" style="height:480px;width:100%">
      <div id="tv_chart_gold" style="height:460px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget({{
        "autosize": true,
        "symbol": "OANDA:XAUUSD",
        "interval": "{tv_tf}",
        "timezone": "Asia/Kolkata",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "enable_publishing": false,
        "allow_symbol_change": false,
        "container_id": "tv_chart_gold"
      }});
      </script>
    </div>
    """
    components.html(tv_widget_html, height=490, scrolling=False)

with col_details:
    st.subheader("Strategy Details")
    if not df_live.empty:
        st.write(f"**Votes:** {strategy_res['votes']}")
        st.write(f"**Stop Loss:** `${strategy_res['sl']:,.2f}` | **Take Profit:** `${strategy_res['tp']:,.2f}`")

st.divider()

st.subheader("Active Gold Positions")
open_trades_df = pd.read_sql_query("SELECT * FROM gold_open_trades ORDER BY id DESC", con)

if open_trades_df.empty:
    st.info("No active gold positions.")
else:
    for idx, r in open_trades_df.iterrows():
        with st.expander(f"Trade #{r['id']} | XAUUSD ({r['bias']} - {r['quantity']} Lot)"):
            if st.button(f"Square Off #{r['id']}", key=f"sq_gold_{r['id']}"):
                pnl = close_trade(con, r['id'], live_p, reason="Manual Square-Off")
                st.toast(f"Trade #{r['id']} closed! PnL: ${pnl:+,.2f}")
                st.rerun()

con.close()
