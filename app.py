import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 — Price Action Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_price_action_bot.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F"},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X"},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X"},
}

INTERVALS = {
    "5 min": {"td": "5min", "yf": "5m", "tv_int": "5", "minutes": 5},
    "15 min": {"td": "15min", "yf": "15m", "tv_int": "15", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "tv_int": "60", "minutes": 60},
    "4 hours": {"td": "240min", "yf": "1h", "tv_int": "240", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "tv_int": "D", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v7 (
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
        CREATE TABLE IF NOT EXISTS trade_history_v7 (
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
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 500.0)")
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

def get_market_data(symbol_label, interval_label, rows=300):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    
    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", ""))
    if td_key and td_key != "demo":
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": info["td"], "interval": cfg["td"], "outputsize": rows, "apikey": td_key},
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
                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "TwelveData Live Feed 🟢"
        except Exception:
            pass

    try:
        import yfinance as yf
        ticker = yf.Ticker(info["yf"])
        df_yf = ticker.history(period="5d", interval=cfg["yf"])
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df_yf["datetime_ist"] = df_yf.index.tz_convert(IST)
            return df_yf[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "Yahoo Finance Fallback Feed 🟡"
    except Exception:
        pass
        
    return pd.DataFrame(), "No Data Available"

def evaluate_price_lesson_supply_demand_strategy(df, live_market_price):
    if len(df) < 30:
        return {"bias":"BUY", "votes":"Default safe bias", "bullish":1, "bearish":0, "entry":live_market_price, "sl":live_market_price*0.99, "tp":live_market_price*1.02}
    
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

    if live_market_price < eq_50:
        bullish_votes.append("Discount Zone (Buy Bias)")
    else:
        bearish_votes.append("Premium Zone (Sell Bias)")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    if lower_wick >= body * 1.2 and last.close > last.open:
        bullish_votes.append("Demand Rejection")
    elif upper_wick >= body * 1.2 and last.close < last.open:
        bearish_votes.append("Supply Rejection")

    if last.close > last.open and prev.close < prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open:
        bearish_votes.append("Bearish Engulfing")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score:
        bias = "BUY"
    elif s_score > b_score:
        bias = "SELL"
    else:
        bias = "BUY" if last.close >= last.open else "SELL"

    sl_buffer = max(rng * 1.5, 2.0 if live_market_price > 100 else 0.0020)
    tp_buffer = max(rng * 3.0, 4.0 if live_market_price > 100 else 0.0040)

    if bias == "BUY":
        sl = live_market_price - sl_buffer
        tp = live_market_price + tp_buffer
    else:
        sl = live_market_price + sl_buffer
        tp = live_market_price - tp_buffer

    all_votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes) if bullish_votes else 'None'} | Bearish [{s_score}]: {', '.join(bearish_votes) if bearish_votes else 'None'}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(live_market_price, 2 if live_market_price > 100 else 5),
        "sl": round(sl, 2 if live_market_price > 100 else 5),
        "tp": round(tp, 2 if live_market_price > 100 else 5)
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

def close_single_trade(con, trade_id, current_price, is_gold=True, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v7 WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    
    if bias == "BUY":
        diff = current_price - entry_price
    else:
        diff = entry_price - current_price
        
    multiplier = 100 if is_gold else 10000
    pnl_usd = round(diff * lot_size * multiplier, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO trade_history_v7 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if is_gold else 5), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v7 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, current_price, timeframe_label, is_gold=True):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM tv_open_trades_v7")
    open_trades = cursor.fetchall()
    
    tf_minutes = INTERVALS[timeframe_label]["minutes"]
    now_utc = datetime.now(timezone.utc)
    
    for tid, symbol, tf, bias, entry, sl, tp, lot, entry_time_str in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close 👤"
        
        if bias == "BUY":
            if current_price >= tp:
                exit_p = tp
                reason = "TP Hitter 🎯"
                closed = True
            elif current_price <= sl:
                exit_p = sl
                reason = "SL Hitter 🛑"
                closed = True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p = tp
                reason = "TP Hitter 🎯"
                closed = True
            elif current_price >= sl:
                exit_p = sl
                reason = "SL Hitter 🛑"
                closed = True
                
        # Time expiry check based on timeframe
        if not closed and tf == timeframe_label:
            try:
                entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                elapsed_mins = (now_utc - entry_dt).total_seconds() / 60.0
                if elapsed_mins >= tf_minutes:
                    exit_p = current_price
                    reason = f"Time Expired ({tf}) ⏱️"
                    closed = True
            except Exception:
                pass
                
        if closed:
            close_single_trade(con, tid, exit_p, is_gold=is_gold, reason=reason)

def manage_auto_trading(con, symbol, timeframe, result, lot_size, candle_ts, current_price, is_gold, market_status, allow_weekend_practice):
    if market_status != "OPEN" and not allow_weekend_practice:
        return

    cursor = con.cursor()
    # Check if a trade was ALREADY opened for THIS exact candle timestamp (Prevents duplicate trade on the same candle)
    cursor.execute("SELECT id FROM tv_open_trades_v7 WHERE symbol = ? AND candle_timestamp = ?", (symbol, str(candle_ts)))
    existing_on_this_candle = cursor.fetchone()

    if not existing_on_this_candle:
        # Open a new trade without killing existing active trades (Allows multiple trades over time/candles)
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO tv_open_trades_v7 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI DESIGN ---
st.title("🪙 BIG MAN2 — Price Action Bot")
st.caption("Multiple Trades Allowed per Session | No Same-Candle Duplicates")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.toast("Refreshed!", icon="🔄")
        st.rerun()
    
    st.markdown("---")
    weekend_practice = st.checkbox("Weekend Practice Mode", value=True)
    auto_mode = st.toggle("Enable Auto-Pilot Bot", value=True)
    
    if st.button("➕ Open Manual Trade", use_container_width=True):
        df_temp, _ = get_market_data(symbol, timeframe)
        if not df_temp.empty:
            live_p = float(df_temp.close.iloc[-1])
            res_temp = evaluate_price_lesson_supply_demand_strategy(df_temp, live_p)
            candle_ts = df_temp.index[-1]
            manage_auto_trading(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts, current_price=live_p, is_gold=("XAU" in symbol), market_status=m_status, allow_weekend_practice=weekend_practice)
            st.sidebar.success("Manual Trade Added!")
            st.rerun()

    st.markdown("---")
    if st.button("🗑️ Clear All Trades & History", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v7")
        con.execute("DELETE FROM trade_history_v7")
        con.commit()
        st.sidebar.success("Cleared!")
        st.rerun()

    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 500.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Reset to $500!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")
df, data_source = get_market_data(symbol, timeframe)

if not df.empty:
    current_live_price = float(df.close.iloc[-1])
    result = evaluate_price_lesson_supply_demand_strategy(df, current_live_price)
    is_gold = "XAU" in symbol
    current_candle_ts = df.index[-1]
    
    # 1. Check TP, SL and Time Expiry for all active trades independently
    check_auto_exit_conditions(con, current_live_price, timeframe, is_gold=is_gold)
    
    # 2. Auto-Pilot execution: Allows multiple active trades across different candles, prevents same-candle duplicates
    if auto_mode:
        manage_auto_trading(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts, current_price=current_live_price, is_gold=is_gold, market_status=m_status, allow_weekend_practice=weekend_practice)

    curr_balance = get_current_balance(con)
    bal_color = "🟢" if curr_balance >= 500 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Live Market Price", f"{current_live_price:.2f}" if is_gold else f"{current_live_price:.5f}")
    col2.metric("Signal Bias", result["bias"])
    col3.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live Chart ({symbol} - {timeframe})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_int = INTERVALS[timeframe]["tv_int"]
    
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_symbol}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" style="width:100%;height:480px;border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader("💡 Confluence Details")
    if not df.empty:
        st.write(f"**Breakdown:** {result['votes']}")
        fmt = ".2f" if "XAU" in symbol else ".5f"
        st.write(f"**Stop Loss:** `{result['sl']:{fmt}}` | **Take Profit:** `{result['tp']:{fmt}}`")
        st.markdown(f"*Source: {data_source}*")

st.divider()
st.subheader("📋 Active Open Trades")

open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, timeframe, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v7 ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📍 **Price:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`")
            if st.button(f"Close #{row['id']}", key=f"close_{row['id']}"):
                current_p = float(df.close.iloc[-1]) if not df.empty else row['entry_price']
                close_single_trade(con, row['id'], current_p, is_gold=("XAU" in row['symbol']), reason="Manual Close 👤")
                st.rerun()

st.divider()
st.subheader("📜 Trade History")
history_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, symbol, timeframe, bias, lot_size, entry_price, exit_price, exit_reason, pnl, strategy_votes FROM trade_history_v7 ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("History khali hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+.2f}"):
            st.markdown(f"**PnL:** `${row['pnl']:+.2f}` | **Reason:** `{row['exit_reason']}`")

con.close()
