import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import requests

# Indian Standard Time (IST) Offset: UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 | Gold XAUUSD Trading Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_gold_bot.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "yf": "GC=F", "td": "XAU/USD"},
    "EUR/USD": {"tv": "FX:EURUSD", "yf": "EURUSD=X", "td": "EUR/USD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "yf": "GBPUSD=X", "td": "GBP/USD"},
}
INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "1h", "minutes": 60},
    "4 hours": {"td": "4h", "yf": "1h", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS auto_trade_journal_gold_v5 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            closed_at_ist TEXT DEFAULT '-',
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_time_utc TIMESTAMP NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.1,
            status TEXT NOT NULL,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            exit_price REAL DEFAULT 0.0,
            pnl REAL DEFAULT 0.0
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
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 500.0)")
    con.commit()
    return con

def get_data(symbol_label, interval_label, rows=250):
    info = SYMBOLS[symbol_label]
    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", ""))
    cfg = INTERVALS[interval_label]
    if td_key:
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": info["td"], "interval": cfg["td"], "outputsize": rows, "apikey": td_key},
                timeout=15,
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
                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "Twelve Data"
        except Exception:
            pass
    try:
        period = "60d" if cfg["yf"] in ("15m", "1h") else "2y"
        df = yf.download(info["yf"], period=period, interval=cfg["yf"], progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame(), "No data"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        df = df[["open", "high", "low", "close"]].dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        df["datetime_ist"] = df.index.tz_convert(IST)
        if interval_label == "4 hours":
            df = df.resample("4h").agg({"open":"first", "high":"max", "low":"min", "close":"last", "datetime_ist":"last"}).dropna()
        return df.tail(rows), "Yahoo Finance fallback"
    except Exception:
        return pd.DataFrame(), "No data"

def evaluate_master_strategies(df):
    if len(df) < 30:
        return {"bias":"WAIT", "votes":"Kam data hai", "bullish":0, "bearish":0, "entry":0, "sl":0, "tp":0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df.high.tail(50).max())
    swing_low = float(df.low.tail(50).min())
    diff = swing_high - swing_low
    
    eq_50 = swing_high - (diff * 0.50)
    ote_618 = swing_high - (diff * 0.618)
    
    bullish_votes = []
    bearish_votes = []

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    if last.close < eq_50:
        bullish_votes.append("Discount Zone")
    else:
        bullish_votes.append("Premium Zone")

    if abs(last.close - ote_618) <= (diff * 0.05):
        if last.close > ote_618:
            bullish_votes.append("Fib 0.618 OTE Buy")
        else:
            bearish_votes.append("Fib 0.618 OTE Sell")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Wick")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Wick")

    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        bearish_votes.append("Bearish Engulfing")

    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    if last.close > prior_high:
        bullish_votes.append("Resistance Breakout")
    elif last.close < prior_low:
        bullish_votes.append("Support Breakdown")

    if body > rng * 0.6:
        if last.close > last.open:
            bullish_votes.append("Momentum Up")
        else:
            bullish_votes.append("Momentum Down")

    sma20 = float(df.close.tail(20).mean())
    sma50 = float(df.close.tail(50).mean()) if len(df) >= 50 else sma20
    if sma20 > sma50:
        bullish_votes.append("Uptrend SMA")
    else:
        bullish_votes.append("Downtrend SMA")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score and b_score >= 2:
        bias = "BUY"
    elif s_score > b_score and s_score >= 2:
        bias = "SELL"
    else:
        bias = "BUY" if last.close > prev.close else "SELL"

    current_close = float(last.close)
    sl_buffer = max(rng * 1.5, 2.0)
    tp_buffer = max(rng * 3.0, 4.0)

    if bias == "BUY":
        sl = current_close - sl_buffer
        tp = current_close + tp_buffer
    else:
        sl = current_close + sl_buffer
        tp = current_close - tp_buffer

    all_votes_str = f"Bull [{b_score}]: {', '.join(bullish_votes)} | Bear [{s_score}]: {', '.join(bearish_votes)}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_close, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2)
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 500.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_auto_trade(con, symbol, timeframe, result, lot_size=0.1):
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO auto_trade_journal_gold_v5 (created_at_ist, closed_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, status, bias, strategy_votes, exit_price, pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, "-", symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, "OPEN", result["bias"], result["votes"], 0.0, 0.0)
    )
    con.commit()

def close_single_trade(con, trade_id, current_price, bias, entry_price, lot_size=0.1):
    if bias == "BUY":
        diff = current_price - entry_price
    else:
        diff = entry_price - current_price
        
    pnl_usd = round(diff * lot_size * 100, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "UPDATE auto_trade_journal_gold_v5 SET status = 'CLOSED', closed_at_ist = ?, exit_price = ?, pnl = ? WHERE id = ?",
        (closed_at_ist, round(current_price, 2), pnl_usd, trade_id)
    )
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, current_price, timeframe_label):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM auto_trade_journal_gold_v5 WHERE status = 'OPEN'")
    open_trades = cursor.fetchall()
    
    tf_minutes = INTERVALS[timeframe_label]["minutes"]
    now_utc = datetime.now(timezone.utc)
    
    for tid, symbol, tf, bias, entry, sl, tp, lot, entry_time_str in open_trades:
        closed = False
        exit_p = current_price
        
        # 1. TP / SL Price Hit Check
        if bias == "BUY":
            if current_price >= tp:
                exit_p = tp
                closed = True
            elif current_price <= sl:
                exit_p = sl
                closed = True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p = tp
                closed = True
            elif current_price >= sl:
                exit_p = sl
                closed = True
                
        # 2. Exact Timeframe Expiry Check (jaise 15 min complete hone par)
        if not closed and tf == timeframe_label:
            try:
                entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                elapsed_mins = (now_utc - entry_dt).total_seconds() / 60.0
                if elapsed_mins >= tf_minutes:
                    exit_p = current_price
                    closed = True
            except Exception:
                pass
                
        if closed:
            close_single_trade(con, tid, exit_p, bias, entry, lot)

# --- STREAMLIT UI ---
st.title("🪙 BIG MAN2 — Gold (XAUUSD) Master Bot")
st.caption("Professional Multi-Trade Engine with Strict Time-Expiry & TP/SL Auto-Close + $500 Balance")

con = connect_db()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.1, step=0.01)
    
    if st.button("🔄 Refresh Market & Auto-Close Trades", use_container_width=True):
        st.toast("Market refreshed & checking time/TP/SL!", icon="🔄")
    
    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot", value=False)
    
    if st.button("➕ Open New Gold Trade", use_container_width=True):
        df_temp, _ = get_data(symbol, timeframe)
        if not df_temp.empty:
            res_temp = evaluate_master_strategies(df_temp)
            execute_auto_trade(con, symbol, timeframe, res_temp, lot_size=lot_input)
            st.sidebar.success(f"Gold trade opened on {timeframe}!")

    st.markdown("---")
    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 500.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset to $500.00!")
        st.rerun()

df, source = get_data(symbol, timeframe)

if not df.empty:
    result = evaluate_master_strategies(df)
    current_live_price = float(df.close.iloc[-1])
    
    # Check Time Expiry and TP/SL automatically on refresh/interaction
    check_auto_exit_conditions(con, current_live_price, timeframe)
    
    if auto_mode:
        execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input)

    curr_balance = get_current_balance(con)
    bal_color = "🟢" if curr_balance >= 500 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Live Gold Rate", f"{current_live_price:.2f}")
    col2.metric("Signal Bias", result["bias"])
    col3.metric("Strategy Votes", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live Gold Chart ({symbol} - {timeframe})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_interval = {"15 min":"15", "1 hour":"60", "4 hours":"240", "1 day":"D"}[timeframe]
    html = f"""
    <div class="tradingview-widget-container" style="height:480px;width:100%">
      <div id="tradingview_chart" style="height:460px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget({{
        "autosize": true,
        "symbol": "{tv_symbol}",
        "interval": "{tv_interval}",
        "timezone": "Asia/Kolkata",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "enable_publishing": false,
        "allow_symbol_change": true,
        "hide_side_toolbar": false,
        "container_id": "tradingview_chart"
      }});
      </script>
    </div>
    """
    components.html(html, height=490, scrolling=False)

with right:
    st.subheader("💡 Gold Market Analysis")
    if not df.empty:
        st.write(f"**Strategy Confluence:** {result['votes']}")
        st.write(f"**Stop Loss:** `{result['sl']:.2f}` | **Take Profit:** `{result['tp']:.2f}`")
        st.markdown("---")
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()

st.subheader("📋 Active Open Trades (Strict Time & TP/SL Expiry)")

open_df = pd.read_sql_query(
    "SELECT id, created_at_ist, symbol, timeframe, bias, lot_size, entry_price, stop_loss, take_profit FROM auto_trade_journal_gold_v5 WHERE status = 'OPEN' ORDER BY id DESC",
    con,
)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 1.5, 1.5, 1])
            c1.markdown(f"**ID #{row['id']} | {row['symbol']}**\n\n🕒 **TF:** `{row['timeframe']}` | **{row['bias']}** ({row['lot_size']} Lot)\n\n📅 `{row['created_at_ist']}`")
            c2.markdown(f"📍 **Entry:** `{row['entry_price']:.2f}`\n🛑 **SL:** `{row['stop_loss']:.2f}`")
            c3.markdown(f"🎯 **TP:** `{row['take_profit']:.2f}`\n⚡ **Status:** `OPEN`")
            if c4.button(f"Close #{row['id']}", key=f"close_open_{row['id']}"):
                current_p = float(df.close.iloc[-1]) if not df.empty else row['entry_price']
                pnl_realized = close_single_trade(con, row['id'], current_p, row['bias'], row['entry_price'], row['lot_size'])
                st.toast(f"Trade #{row['id']} closed! PnL: ${pnl_realized:+.2f}")
                st.rerun()

st.divider()

st.subheader("📜 Closed Trades History")
closed_df = pd.read_sql_query(
    "SELECT id, created_at_ist, closed_at_ist, symbol, timeframe, bias, lot_size, entry_price, exit_price, pnl FROM auto_trade_journal_gold_v5 WHERE status = 'CLOSED' ORDER BY id DESC LIMIT 30",
    con,
)

if closed_df.empty:
    st.info("Abhi koi closed trade history nahi hai.")
else:
    closed_df.columns = ['ID', 'Entry Time', 'Exit Time', 'Pair', 'TF', 'Bias', 'Lot', 'Entry', 'Exit', 'PnL ($)']
    st.dataframe(closed_df, use_container_width=True)

con.close()
