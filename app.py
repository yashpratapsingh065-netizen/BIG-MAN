import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 — Price Lesson Master Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_price_action_bot.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F", "mt5": "XAUUSD"},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X", "mt5": "EURUSD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X", "mt5": "GBPUSD"},
}

INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m", "tv_int": "15", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "tv_int": "60", "minutes": 60},
    "4 hours": {"td": "240min", "yf": "1h", "tv_int": "240", "minutes": 240},
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v10 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_time_utc TIMESTAMP NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.01,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history_v10 (
            id INTEGER PRIMARY KEY,
            created_at_ist TEXT NOT NULL,
            closed_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL,
            bias TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS account_balance (
            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 3000.0)")
    con.commit()
    return con

def get_forex_market_status():
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()
    hour = now_utc.hour
    if weekday == 5:
        return "CLOSED", "🔴 Market Closed (Weekend Saturday)"
    elif weekday == 6 and hour < 22:
        return "CLOSED", "🔴 Market Closed (Weekend Sunday)"
    elif weekday == 4 and hour >= 22:
        return "CLOSED", "🔴 Market Closed (Weekend Friday Evening)"
    else:
        return "OPEN", "🟢 International Forex/Gold Market Live"

def get_market_data(symbol_label, interval_td, rows=150):
    info = SYMBOLS[symbol_label]
    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", ""))
    
    if td_key:
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": info["td"], "interval": interval_td, "outputsize": rows, "apikey": td_key},
                timeout=8,
            )
            payload = resp.json()
            if "values" in payload:
                df = pd.DataFrame(payload["values"])
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
                df["datetime_ist"] = df["datetime"].dt.tz_convert(IST)
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
                df = df.sort_values("datetime").set_index("datetime")
                live_price = float(df.close.iloc[-1])
                return df[["open", "high", "low", "close", "datetime_ist"]], live_price
        except Exception:
            pass
    return pd.DataFrame(), 0.0

def analyze_higher_timeframe_bias(symbol_label):
    """
    Price Lesson Multi-Timeframe Rule: 
    Analyzes 1-Hour (1h) structure to determine institutional bias (BUY or SELL).
    """
    df_1h, _ = get_market_data(symbol_label, "1h", rows=50)
    if df_1h.empty or len(df_1h) < 10:
        return "NEUTRAL"
    
    # Simple Structure check using recent swing highs/lows on 1H
    recent_closes = df_1h["close"].tail(10)
    if recent_closes.iloc[-1] > recent_closes.iloc[0]:
        return "BUY"
    elif recent_closes.iloc[-1] < recent_closes.iloc[0]:
        return "SELL"
    return "NEUTRAL"

def evaluate_price_lesson_strategy(df_m15, live_market_price, htf_bias, is_gold):
    if len(df_m15) < 30:
        return {"bias":"HOLD", "votes":"Insufficient Data", "bullish":0, "bearish":0, "entry":live_market_price, "sl":live_market_price, "tp":live_market_price}
    
    last = df_m15.iloc[-1]
    prev = df_m15.iloc[-2]
    p_prev = df_m15.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df_m15.high.tail(40).max())
    swing_low = float(df_m15.low.tail(40).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    # 1. Premium / Discount Zone (S5/S6)
    if live_market_price < eq_50:
        bullish_votes.append("Discount Zone (< 0.5 Eq)")
    else:
        bearish_votes.append("Premium Zone (> 0.5 Eq)")

    # 2. Fair Value Gap (FVG) Check (S5-FVG)
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    # 3. Supply & Demand Rejection Wicks (>= 1.3 ratio rule)
    if lower_wick >= body * 1.3 and last.close > last.open:
        bullish_votes.append("Demand Rejection")
    elif upper_wick >= body * 1.3 and last.close < last.open:
        bullish_votes.append("Supply Rejection")

    # 4. Candlestick Engulfing Pattern (S3)
    if last.close > last.open and prev.close < prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open:
        bearish_votes.append("Bearish Engulfing")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    # STRICT MULTI-TIMEFRAME FILTER: 
    # M15 signal MUST align with 1H Higher Timeframe (HTF) Bias!
    m15_bias = "HOLD"
    if b_score >= 2 and b_score > s_score:
        m15_bias = "BUY"
    elif s_score >= 2 and s_score > b_score:
        m15_bias = "SELL"

    # Final Decision based on HTF Harmony
    final_bias = "HOLD"
    if m15_bias == "BUY" and htf_bias in ["BUY", "NEUTRAL"]:
        final_bias = "BUY"
    elif m15_bias == "SELL" and htf_bias in ["SELL", "NEUTRAL"]:
        final_bias = "SELL"

    sl_buffer = max(rng * 1.2, 2.5 if is_gold else 0.0015)
    tp_buffer = sl_buffer * 2.5

    if final_bias == "BUY":
        sl = live_market_price - sl_buffer
        tp = live_market_price + tp_buffer
    elif final_bias == "SELL":
        sl = live_market_price + sl_buffer
        tp = live_market_price - tp_buffer
    else:
        sl = live_market_price
        tp = live_market_price

    votes_str = f"HTF 1H Bias: [{htf_bias}] | Bull [{b_score}]: {', '.join(bullish_votes)} | Bear [{s_score}]: {', '.join(bearish_votes)}"
    decimals = 2 if is_gold else 5

    return {
        "bias": final_bias,
        "votes": votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(live_market_price, decimals),
        "sl": round(sl, decimals),
        "tp": round(tp, decimals)
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 3000.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def close_single_trade(con, trade_id, current_price, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v10 WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    is_gold = "XAU" in symbol
    
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    multiplier = 100 if is_gold else 10000
    pnl_usd = round(diff * lot_size * multiplier, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    decimals = 2 if is_gold else 5
    
    con.execute(
        "INSERT INTO trade_history_v10 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, decimals), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v10 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, symbol_label, current_price):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, stop_loss, take_profit FROM tv_open_trades_v10 WHERE symbol = ?", (symbol_label,))
    open_trades = cursor.fetchall()
    
    for tid, bias, sl, tp in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close"
        
        if bias == "BUY":
            if current_price >= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price <= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price >= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
                
        if closed:
            close_single_trade(con, tid, exit_p, reason=reason)

def manage_auto_trading(con, symbol, result, lot_size, candle_ts, market_status):
    if market_status != "OPEN" or result["bias"] == "HOLD":
        return

    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v10 WHERE symbol = ? AND candle_timestamp = ?", (symbol, str(candle_ts)))
    if not cursor.fetchone():
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO tv_open_trades_v10 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, "15 min", entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI INTERFACE ---
st.title("🪙 BIG MAN2 — Price Lesson MTF Master Bot")
st.caption("MetaQuotes-Demo (5058003871) • 1H Trend + M15 Zone Harmonizer • Strict 1:2.5 RRR")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()
    
    auto_mode = st.toggle("Enable Master Auto-Pilot", value=True)
    is_gold = "XAU" in symbol
    
    df_m15_temp, live_p_temp = get_market_data(symbol, "15min", rows=100)
    htf_bias_temp = analyze_higher_timeframe_bias(symbol)
    
    if st.button("➕ Force Manual Trade (Check Bias)", use_container_width=True):
        if not df_m15_temp.empty and live_p_temp > 0:
            res_temp = evaluate_price_lesson_strategy(df_m15_temp, live_p_temp, htf_bias_temp, is_gold)
            if res_temp["bias"] != "HOLD":
                manage_auto_trading(con, symbol, res_temp, lot_input, df_m15_temp.index[-1], m_status)
                st.sidebar.success(f"Trade Placed with {res_temp['bias']}!")
            else:
                st.sidebar.warning("Trade Blocked! 1H Trend and M15 are conflicting.")
            st.rerun()

    if st.button("🗑️ Clear All Trades", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v10")
        con.execute("DELETE FROM trade_history_v10")
        con.commit()
        st.rerun()

st.info(f"Market Status: **{m_text}**")

# Fetch data for execution
df_m15, current_live_price = get_market_data(symbol, "15min", rows=100)
htf_bias = analyze_higher_timeframe_bias(symbol)
is_gold = "XAU" in symbol

if not df_m15.empty and current_live_price > 0:
    result = evaluate_price_lesson_strategy(df_m15, current_live_price, htf_bias, is_gold)
    current_candle_ts = df_m15.index[-1]
    
    check_auto_exit_conditions(con, symbol, current_live_price)
    
    if auto_mode:
        manage_auto_trading(con, symbol, result, lot_size=lot_input, candle_ts=current_candle_ts, market_status=m_status)

    curr_balance = get_current_balance(con)
    st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD**")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Live Price", f"{current_live_price:.2f}" if is_gold else f"{current_live_price:.5f}")
    col2.metric("1H Trend (HTF)", htf_bias)
    col3.metric("M15 Signal Bias", result["bias"])
    col4.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()
left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live Chart ({symbol} - 15m)")
    tv_symbol = SYMBOLS[symbol]["tv"]
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_symbol}&interval=15&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&theme=dark&style=1&timezone=Asia%2FKolkata" style="width:100%;height:480px;border:none;"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader("💡 Price Lesson Breakdown")
    if not df_m15.empty:
        st.write(f"**Analysis:** {result['votes']}")
        fmt = ".2f" if is_gold else ".5f"
        st.write(f"**Stop Loss:** `{result['sl']:{fmt}}` | **Take Profit:** `{result['tp']:{fmt}}`")

st.divider()
st.subheader("📋 Active Open Trades")
open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, timeframe, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v10 ORDER BY id DESC", con)

if open_df.empty:
    st.info("कोई ट्रेड ओपन नहीं है (जब तक 1H और M15 का ट्रेंड मैच नहीं होगा, बोट सेफ मोड पर HOLD रहेगा)।")
else:
    for index, row in open_df.iterrows():
        row_gold = "XAU" in row['symbol']
        fmt = ".2f" if row_gold else ".5f"
        with st.expander(f"📌 Trade #{row['id']} | {row['symbol']} ({row['bias']})"):
            st.markdown(f"**Entry:** `{row['entry_price']:{fmt}}` | **SL:** `{row['stop_loss']:{fmt}}` | **TP:** `{row['take_profit']:{fmt}}`")
            if st.button(f"Close #{row['id']}", key=f"cl_{row['id']}"):
                close_single_trade(con, row['id'], current_live_price, reason="Manual Close")
                st.rerun()

st.divider()
st.subheader("📜 Trade History")
history_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, symbol, bias, lot_size, pnl, exit_reason FROM trade_history_v10 ORDER BY id DESC LIMIT 30", con)
if history_df.empty:
    st.info("इतिहास खाली है।")
else:
    for index, row in history_df.iterrows():
        color = "🟢" if row['pnl'] >= 0 else "🔴"
        st.markdown(f"{color} **ID #{row['id']}** | {row['symbol']} ({row['bias']}) | PnL: `${row['pnl']:+.2f}` | Reason: `{row['exit_reason']}`")

con.close()
