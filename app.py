import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 — Independent Multi-Timeframe Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_price_action_bot_v15.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F", "mt5": "XAUUSD"},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X", "mt5": "EURUSD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X", "mt5": "GBPUSD"},
}

INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m", "tv_int": "15", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "tv_int": "60", "minutes": 60},
    "4 hours": {"td": "240min", "yf": "1h", "tv_int": "240", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "tv_int": "D", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v15 (
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
        CREATE TABLE IF NOT EXISTS trade_history_v15 (
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

def get_market_data(symbol_label, interval_td, rows=200):
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

def run_sqlite_backtest_optimization(con, symbol_label, timeframe_label):
    df_hist = pd.read_sql_query("SELECT bias, pnl, exit_reason FROM trade_history_v15 WHERE symbol = ? AND timeframe = ?", con, params=(symbol_label, timeframe_label))
    if df_hist.empty:
        return {"win_rate": 0.0, "total_trades": 0, "penalty_bias": "NEUTRAL"}
    
    total = len(df_hist)
    wins = len(df_hist[df_hist["pnl"] > 0])
    win_rate = (wins / total) * 100 if total > 0 else 0.0
    
    recent_losses = df_hist.tail(3)
    penalty = "NEUTRAL"
    if len(recent_losses[recent_losses["exit_reason"].str.contains("SL")]) >= 2:
        penalty = recent_losses.iloc[-1]["bias"]
        
    return {"win_rate": win_rate, "total_trades": total, "penalty_bias": penalty}

def evaluate_price_lesson_strategy(df_current, live_market_price, backtest_insights, is_gold):
    if len(df_current) < 30:
        return {"bias":"HOLD", "votes":"Insufficient Data", "bullish":0, "bearish":0, "entry":live_market_price, "sl":live_market_price, "tp":live_market_price}
    
    last = df_current.iloc[-1]
    prev = df_current.iloc[-2]
    p_prev = df_current.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df_current.high.tail(40).max())
    swing_low = float(df_current.low.tail(40).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    if live_market_price < eq_50:
        bullish_votes.append("Discount Zone (< 0.5 Eq)")
    else:
        bearish_votes.append("Premium Zone (> 0.5 Eq)")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    if lower_wick >= body * 1.3 and last.close > last.open:
        bullish_votes.append("Demand Rejection")
    elif upper_wick >= body * 1.3 and last.close < last.open:
        bullish_votes.append("Supply Rejection")

    if last.close > last.open and prev.close < prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open:
        bearish_votes.append("Bearish Engulfing")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    final_bias = "HOLD"
    if b_score >= 1 and b_score > s_score and backtest_insights["penalty_bias"] != "BUY":
        final_bias = "BUY"
    elif s_score >= 1 and s_score > b_score and backtest_insights["penalty_bias"] != "SELL":
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

    votes_str = f"Bull [{b_score}]: {', '.join(bullish_votes)} | Bear [{s_score}]: {', '.join(bearish_votes)}"
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
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v15 WHERE id = ?", (trade_id,))
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
        "INSERT INTO trade_history_v15 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, decimals), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v15 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, symbol_label, current_price):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, stop_loss, take_profit FROM tv_open_trades_v15 WHERE symbol = ?", (symbol_label,))
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

def manage_auto_trading(con, symbol, timeframe_label, result, lot_size, candle_ts, market_status):
    if market_status != "OPEN" or result["bias"] == "HOLD":
        return

    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v15 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe_label, str(candle_ts)))
    if not cursor.fetchone():
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO tv_open_trades_v15 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe_label, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI INTERFACE ---
st.title("🪙 BIG MAN2 — Independent Multi-Timeframe Bot")
st.caption("MetaQuotes-Demo (5058003871) • Independent Timeframe Execution Engine • 1:2.5 RRR")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    active_chart_tf = st.selectbox("Active Chart View", list(INTERVALS.keys()), index=0)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()
    
    auto_mode = st.toggle("Enable Independent Auto-Pilot", value=True)
    is_gold = "XAU" in symbol
    
    if st.button("🗑️ Clear All Trades & History", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v15")
        con.execute("DELETE FROM trade_history_v15")
        con.commit()
        st.rerun()

st.info(f"Market Status: **{m_text}**")

# Background Independent Scanning across ALL timeframes if Auto-Mode is ON
is_gold = "XAU" in symbol
live_price_global = 0.0

if auto_mode and m_status == "OPEN":
    for tf_lbl, tf_cfg in INTERVALS.items():
        df_scan, live_p_scan = get_market_data(symbol, tf_cfg["td"], rows=150)
        if not df_scan.empty and live_p_scan > 0:
            if tf_lbl == active_chart_tf:
                live_price_global = live_p_scan
            backtest_ins = run_sqlite_backtest_optimization(con, symbol, tf_lbl)
            res_scan = evaluate_price_lesson_strategy(df_scan, live_p_scan, backtest_ins, is_gold)
            if res_scan["bias"] != "HOLD":
                manage_auto_trading(con, symbol, tf_lbl, res_scan, lot_size=lot_input, candle_ts=df_scan.index[-1], market_status=m_status)

# Fallback live price if global wasn't set
if live_price_global == 0.0:
    _, live_price_global = get_market_data(symbol, INTERVALS[active_chart_tf]["td"], rows=50)

check_auto_exit_conditions(con, symbol, live_price_global)

curr_balance = get_current_balance(con)
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD**")

# Display Active Chart Data for selected view
cfg_active = INTERVALS[active_chart_tf]
df_active, _ = get_market_data(symbol, cfg_active["td"], rows=150)
backtest_active = run_sqlite_backtest_optimization(con, symbol, active_chart_tf)
result_active = evaluate_price_lesson_strategy(df_active, live_price_global, backtest_active, is_gold)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Live Price", f"{live_price_global:.2f}" if is_gold else f"{live_price_global:.5f}")
col2.metric(f"{active_chart_tf} Win Rate", f"{backtest_active['win_rate']:.1f}%")
col3.metric(f"{active_chart_tf} Bias", result_active["bias"])
col4.metric("Confluence Score", f"Bull: {result_active['bullish']} | Bear: {result_active['bearish']}")

st.divider()
left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live Chart ({symbol} - {active_chart_tf})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_int = INTERVALS[active_chart_tf]["tv_int"]
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_symbol}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&theme=dark&style=1&timezone=Asia%2FKolkata" style="width:100%;height:480px;border:none;"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader(f"💡 {active_chart_tf} Breakdown")
    if not df_active.empty:
        st.write(f"**Analysis:** {result_active['votes']}")
        fmt = ".2f" if is_gold else ".5f"
        st.write(f"**Stop Loss:** `{result_active['sl']:{fmt}}` | **Take Profit:** `{result_active['tp']:{fmt}}`")

st.divider()
st.subheader("📂 Dedicated Timeframe Sections (Active Trades & History)")

tab_15m, tab_1h, tab_4h, tab_1d = st.tabs(["⏱️ 15 Min Section", "⏰ 1 Hour Section", "📅 4 Hours Section", "🗓️ 1 Day Section"])

tf_tabs_mapping = {
    "⏱️ 15 Min Section": "15 min",
    "⏰ 1 Hour Section": "1 hour",
    "📅 4 Hours Section": "4 hours",
    "🗓️ 1 Day Section": "1 day"
}

for tab_obj, tf_name in zip([tab_15m, tab_1h, tab_4h, tab_1d], tf_tabs_mapping.values()):
    with tab_obj:
        st.markdown(f"### 📌 {tf_name} Active Trades")
        open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v15 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC", con, params=(symbol, tf_name))
        
        if open_df.empty:
            st.info(f"[{tf_name}] कोई सक्रिय ट्रेड नहीं है।")
        else:
            for index, row in open_df.iterrows():
                row_gold = "XAU" in symbol
                fmt = ".2f" if row_gold else ".5f"
                with st.expander(f"Trade #{row['id']} | {row['bias']} ({row['lot_size']} Lot)"):
                    st.markdown(f"**Entry:** `{row['entry_price']:{fmt}}` | **SL:** `{row['stop_loss']:{fmt}}` | **TP:** `{row['take_profit']:{fmt}}`")
                    if st.button(f"Close #{row['id']}", key=f"cl_{tf_name}_{row['id']}"):
                        close_single_trade(con, row['id'], live_price_global, reason="Manual Close")
                        st.rerun()
                        
        st.markdown(f"### 📜 {tf_name} Trade History")
        hist_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, bias, pnl, exit_reason FROM trade_history_v15 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC LIMIT 20", con, params=(symbol, tf_name))
        
        if hist_df.empty:
            st.info(f"[{tf_name}] इतिहास खाली है।")
        else:
            for index, row in hist_df.iterrows():
                color = "🟢" if row['pnl'] >= 0 else "🔴"
                st.markdown(f"{color} **ID #{row['id']}** ({row['bias']}) | PnL: `${row['pnl']:+.2f}` | Reason: `{row['exit_reason']}`")

con.close()
