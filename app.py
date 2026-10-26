import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

# Indian Standard Time (IST) Offset: UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 — TradingView Synchronized Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_tradingview_bot.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F"},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X"},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X"},
}
INTERVALS = {
    "5 min": {"td": "5min", "yf": "5m", "minutes": 5},
    "15 min": {"td": "15min", "yf": "15m", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "minutes": 60},
    "4 hours": {"td": "4h", "yf": "1h", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v3 (
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
        CREATE TABLE IF NOT EXISTS trade_history (
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
    
    if weekday == 5: # Saturday
        return "CLOSED", "🔴 Market Closed (Weekend Saturday)"
    elif weekday == 6 and hour < 22: # Sunday before market open
        return "CLOSED", "🔴 Market Closed (Weekend Sunday)"
    elif weekday == 4 and hour >= 22: # Friday after market close
        return "CLOSED", "🔴 Market Closed (Weekend Friday Evening)"
    else:
        return "OPEN", "🟢 International Forex/Gold Market Live"

def get_tradingview_matched_data(symbol_label, interval_label, rows=250):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    
    try:
        import yfinance as yf
        ticker = yf.Ticker(info["yf"])
        # Fetching latest data with 1d period / 1m interval for absolute live precision
        df_yf = ticker.history(period="2d", interval=cfg["yf"])
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df_yf["datetime_ist"] = df_yf.index.tz_convert(IST)
            return df_yf[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "Yahoo Finance Live Feed 🟢"
    except Exception:
        pass

    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", "demo"))
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
            return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "TwelveData Direct 🟢"
    except Exception:
        pass
        
    return pd.DataFrame(), "No Data"

def evaluate_price_lesson_strategies(df, live_market_price):
    if len(df) < 30:
        return {"bias":"WAIT", "votes":"Kam data uplabdh hai", "bullish":0, "bearish":0, "entry":live_market_price, "sl":live_market_price*0.99, "tp":live_market_price*1.02}
    
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
        bullish_votes.append("Discount Zone (Cheap Area - Buy Bias)")
    else:
        bearish_votes.append("Premium Zone (Expensive Area - Sell Bias)")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Unmitigated Bullish FVG Imbalance")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Unmitigated Bearish FVG Imbalance")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Zone Strong Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Zone Strong Wick Rejection")

    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing Pattern")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        bearish_votes.append("Bearish Engulfing Pattern")

    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    if live_market_price > prior_high:
        bullish_votes.append("Resistance High Breakout")
    elif live_market_price < prior_low:
        bearish_votes.append("Support Low Breakdown")

    if live_market_price >= last.open:
        bullish_votes.append("Bullish Momentum")
    else:
        bearish_votes.append("Bearish Momentum")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score:
        bias = "BUY"
    elif s_score > b_score:
        bias = "SELL"
    else:
        bias = "BUY" if live_market_price >= last.open else "SELL"

    sl_buffer = max(rng * 1.5, 2.0 if live_market_price > 100 else 0.0020)
    tp_buffer = max(rng * 3.0, 4.0 if live_market_price > 100 else 0.0040)

    if bias == "BUY":
        sl = live_market_price - sl_buffer
        tp = live_market_price + tp_buffer
    else:
        sl = live_market_price + sl_buffer
        tp = live_market_price - tp_buffer

    all_votes_str = f"Bullish Confluence [{b_score}]: {', '.join(bullish_votes)} | Bearish Confluence [{s_score}]: {', '.join(bearish_votes)}"

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

def execute_auto_trade(con, symbol, timeframe, result, lot_size, candle_ts, market_status="OPEN", allow_weekend_practice=True):
    if market_status != "OPEN" and not allow_weekend_practice:
        return False, "Market Closed! Enable Weekend Practice Mode in sidebar to test."
        
    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v3 WHERE symbol = ?", (symbol,))
    if cursor.fetchone():
        return False, "Active trade already exists for this symbol!"
        
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO tv_open_trades_v3 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
    )
    con.commit()
    return True, "Trade opened successfully!"

def close_single_trade(con, trade_id, current_price, is_gold=True, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v3 WHERE id = ?", (trade_id,))
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
        "INSERT INTO trade_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if is_gold else 5), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v3 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, current_price, timeframe_label, is_gold=True):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM tv_open_trades_v3")
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

# --- STREAMLIT UI ---
st.title("🪙 BIG MAN2 — TradingView Synchronized Bot")
st.caption("Anti-Duplicate Protection, Price Lesson Strategies & Permanent Trade History")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Refresh TV Market Data", use_container_width=True):
        st.toast("Synced with TradingView price feed!", icon="🔄")
        st.rerun()
    
    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    weekend_practice = st.checkbox("Allow Weekend Practice/Testing Mode", value=True)
    auto_mode = st.toggle("Enable Auto-Pilot", value=False)
    
    if st.button("➕ Open New TV Trade", use_container_width=True):
        df_temp, _ = get_tradingview_matched_data(symbol, timeframe)
        if not df_temp.empty:
            live_p = float(df_temp.close.iloc[-1])
            res_temp = evaluate_price_lesson_strategies(df_temp, live_p)
            candle_ts = df_temp.index[-1]
            success, msg = execute_auto_trade(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts, market_status=m_status, allow_weekend_practice=weekend_practice)
            if success:
                st.sidebar.success(f"Trade opened at {live_p:.2f}!")
                st.rerun()
            else:
                st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🗑️ Clear All Open Trades & History", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v3")
        con.execute("DELETE FROM trade_history")
        con.commit()
        st.sidebar.success("All data cleared successfully!")
        st.rerun()

    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 500.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset to $500.00!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")
df, data_source = get_tradingview_matched_data(symbol, timeframe)

if not df.empty:
    current_live_price = float(df.close.iloc[-1])
    result = evaluate_price_lesson_strategies(df, current_live_price)
    is_gold = "XAU" in symbol
    current_candle_ts = df.index[-1]
    
    check_auto_exit_conditions(con, current_live_price, timeframe, is_gold=is_gold)
    
    if auto_mode and (m_status == "OPEN" or weekend_practice):
        execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts, market_status=m_status, allow_weekend_practice=weekend_practice)

    curr_balance = get_current_balance(con)
    bal_color = "🟢" if curr_balance >= 500 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Live TradingView Price", f"{current_live_price:.2f}" if is_gold else f"{current_live_price:.5f}")
    col2.metric("Signal Bias", result["bias"])
    col3.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol} - {timeframe})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_interval = {"5 min": "5", "15 min": "15", "1 hour": "60", "4 hours": "240", "1 day": "D"}[timeframe]
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
    st.subheader("💡 Price Lesson Video Confluence")
    if not df.empty:
        st.write(f"**Strategy Breakdown:** {result['votes']}")
        fmt = ".2f" if "XAU" in symbol else ".5f"
        st.write(f"**Stop Loss:** `{result['sl']:{fmt}}` | **Take Profit:** `{result['tp']:{fmt}}`")
        st.markdown(f"*Feed Source: {data_source}*")
        st.markdown("---")
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()

st.subheader("📋 Active Open Trades")

open_df = pd.read_sql_query(
    "SELECT id, created_at_ist, symbol, timeframe, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v3 ORDER BY id DESC",
    con,
)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot) - TF: {row['timeframe']}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n📍 **Entry Price:** `{row['entry_price']:{fmt}}`\n🛑 **Stop Loss:** `{row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **Take Profit:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            
            if st.button(f"Close Trade #{row['id']} Now", key=f"close_open_{row['id']}"):
                current_p = float(df.close.iloc[-1]) if not df.empty else row['entry_price']
                pnl_realized = close_single_trade(con, row['id'], current_p, is_gold=("XAU" in row['symbol']), reason="Manual Close 👤")
                st.toast(f"Trade #{row['id']} closed! PnL: ${pnl_realized:+.2f}")
                st.rerun()

st.divider()

st.subheader("📜 Trade History (Permanent Storage)")

history_df = pd.read_sql_query(
    "SELECT id, created_at_ist, closed_at_ist, symbol, timeframe, bias, lot_size, entry_price, exit_price, exit_reason, pnl, strategy_votes FROM trade_history ORDER BY id DESC LIMIT 50",
    con,
)

if history_df.empty:
    st.info("Abhi tak koi trade history me saved nahi hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+.2f} | Reason: {row['exit_reason']}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n🕒 **Exit Time:** `{row['closed_at_ist']}`\n⏱️ **Timeframe:** `{row['timeframe']}`\n📦 **Lot Size:** `{row['lot_size']}`")
            c2.markdown(f"📍 **Entry Price:** `{row['entry_price']:{fmt}}`\n🏁 **Exit Price:** `{row['exit_price']:{fmt}}`\n🏷️ **Exit Reason:** `{row['exit_reason']}`\n💰 **Net PnL:** `${row['pnl']:+.2f}`")
            st.markdown(f"📊 **Strategy Confluence at Entry:** `{row['strategy_votes']}`")

con.close()
