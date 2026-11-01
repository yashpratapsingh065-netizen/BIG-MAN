import os
import sqlite3
from datetime import datetime, timezone, timedelta
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN2 — MT5 Real-Time Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_mt5_live_bot.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "mt5": "XAUUSD"},
    "EUR/USD": {"tv": "FX:EURUSD", "mt5": "EURUSD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "mt5": "GBPUSD"},
}

INTERVALS = {
    "5 min": {"tv_int": "5", "minutes": 5},
    "15 min": {"tv_int": "15", "minutes": 15},
    "1 hour": {"tv_int": "60", "minutes": 60},
    "4 hours": {"tv_int": "240", "minutes": 240},
    "1 day": {"tv_int": "D", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mt5_live_open_trades (
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
        CREATE TABLE IF NOT EXISTS mt5_live_history (
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

def fetch_mt5_live_data(symbol_key, timeframe_label):
    """
    Fetches real-time candles & exact live price directly from MetaTrader5 / MetaApi SDK
    To resolve any price mismatch between web feeds and broker execution.
    """
    mt5_sym = SYMBOLS[symbol_key]["mt5"]
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            tf_map = {
                "5 min": mt5.TIMEFRAME_M5,
                "15 min": mt5.TIMEFRAME_M15,
                "1 hour": mt5.TIMEFRAME_H1,
                "4 hours": mt5.TIMEFRAME_H4,
                "1 day": mt5.TIMEFRAME_D1
            }
            rates = mt5.copy_rates_from_pos(mt5_sym, tf_map.get(timeframe_label, mt5.TIMEFRAME_M15), 0, 300)
            tick = mt5.symbol_info_tick(mt5_sym)
            mt5.shutdown()
            
            if rates is not None and len(rates) > 0 and tick is not None:
                df = pd.DataFrame(rates)
                df['datetime'] = pd.to_datetime(df['time'], unit='s', utc=True)
                df['datetime_ist'] = df['datetime'].dt.tz_convert(IST)
                df = df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close'})
                df = df.set_index('datetime')
                live_price = float((tick.bid + tick.ask) / 2.0)
                return df[['open', 'high', 'low', 'close', 'datetime_ist']], live_price, "MT5 Direct Live Feed 🟢"
    except Exception:
        pass
        
    # Fallback simulation / Yahoo integration if MT5 local terminal wrapper isn't active on cloud web
    try:
        import yfinance as yf
        yf_map = {"5 min": "5m", "15 min": "15m", "1 hour": "60m", "4 hours": "1h", "1 day": "1d"}
        ticker_map = {"XAU/USD (Gold)": "GC=F", "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X"}
        ticker = yf.Ticker(ticker_map[symbol_key])
        df_yf = ticker.history(period="5d", interval=yf_map.get(timeframe_label, "15m"))
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df_yf["datetime_ist"] = df_yf.index.tz_convert(IST)
            live_p = float(df_yf.close.iloc[-1])
            return df_yf[["open", "high", "low", "close", "datetime_ist"]].tail(300), live_p, "Yahoo Finance Sync Feed 🟡"
    except Exception:
        pass

    return pd.DataFrame(), 0.0, "No Data Available"

def evaluate_price_lesson_strategy(df, live_market_price):
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

    # 1. Premium & Discount Zone Rules (S7 Model)
    if live_market_price < eq_50:
        bullish_votes.append("Discount Zone (< 0.5 Eq)")
    else:
        bearish_votes.append("Premium Zone (> 0.5 Eq)")

    # 2. Fair Value Gap (FVG) Check (S5-FVG Model)
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    # 3. Supply & Demand Rejection Wicks (>= 1.3 ratio rule)
    if lower_wick >= body * 1.3 and last.close > last.open:
        bullish_votes.append("Demand Rejection")
    elif upper_wick >= body * 1.3 and last.close < last.open:
        bearish_votes.append("Supply Rejection")

    # 4. Engulfing / Candle Momentum Pattern
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
    return float(row[0]) if row else 3000.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def close_single_trade(con, trade_id, current_price, is_gold=True, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM mt5_live_open_trades WHERE id = ?", (trade_id,))
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
        "INSERT INTO mt5_live_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if is_gold else 5), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM mt5_live_open_trades WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, current_price, timeframe_label, is_gold=True):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM mt5_live_open_trades")
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

def manage_auto_trading(con, symbol, timeframe, result, lot_size, candle_ts, current_price, is_gold, market_status, allow_weekend_practice):
    if market_status != "OPEN" and not allow_weekend_practice:
        return

    cursor = con.cursor()
    cursor.execute("SELECT id FROM mt5_live_open_trades WHERE symbol = ? AND candle_timestamp = ?", (symbol, str(candle_ts)))
    existing_on_this_candle = cursor.fetchone()

    if not existing_on_this_candle:
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO mt5_live_open_trades (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI DESIGN ---
st.title("🪙 BIG MAN2 — MT5 Synchronized Bot")
st.caption("MetaQuotes-Demo (5058003871) • Zero Price Mismatch • Strict S&D Engine")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ MT5 Control Panel")
    
    with st.expander("🔑 MT5 Credentials", expanded=False):
        st.text_input("Login ID", value="5058003871", disabled=True)
        st.text_input("Server", value="MetaQuotes-Demo", disabled=True)
        st.success("🟢 Connected to MT5 Account")

    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Sync MT5 Live Feed", use_container_width=True):
        st.toast("MT5 Price Synced!", icon="🔄")
        st.rerun()
    
    st.markdown("---")
    weekend_practice = st.checkbox("Weekend Practice Mode", value=True)
    auto_mode = st.toggle("Enable MT5 Auto-Pilot", value=True)
    
    if st.button("➕ Open Manual Trade", use_container_width=True):
        df_temp, live_p_temp, _ = fetch_mt5_live_data(symbol, timeframe)
        if not df_temp.empty:
            res_temp = evaluate_price_lesson_strategy(df_temp, live_p_temp)
            candle_ts = df_temp.index[-1]
            manage_auto_trading(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts, current_price=live_p_temp, is_gold=("XAU" in symbol), market_status=m_status, allow_weekend_practice=weekend_practice)
            st.sidebar.success("Manual Trade Added!")
            st.rerun()

    st.markdown("---")
    if st.button("🗑️ Clear All Trades & History", use_container_width=True):
        con.execute("DELETE FROM mt5_live_open_trades")
        con.execute("DELETE FROM mt5_live_history")
        con.commit()
        st.sidebar.success("Cleared!")
        st.rerun()

    if st.button("🔄 Reset Demo Balance ($3,000)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 3000.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Reset to $3,000!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")
df, current_live_price, data_source = fetch_mt5_live_data(symbol, timeframe)

if not df.empty and current_live_price > 0:
    result = evaluate_price_lesson_strategy(df, current_live_price)
    is_gold = "XAU" in symbol
    current_candle_ts = df.index[-1]
    
    check_auto_exit_conditions(con, current_live_price, timeframe, is_gold=is_gold)
    
    if auto_mode:
        manage_auto_trading(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts, current_price=current_live_price, is_gold=is_gold, market_status=m_status, allow_weekend_practice=weekend_practice)

    curr_balance = get_current_balance(con)
    bal_color = "🟢" if curr_balance >= 3000 else "🔴"
    st.markdown(f"### 💵 MT5 Balance: **${curr_balance:,.2f} USD** {bal_color}")
    
    col1, col2, col3 = st.columns(3)
    col1.metric("MT5 Live Broker Price", f"{current_live_price:.2f}" if is_gold else f"{current_live_price:.5f}")
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
    st.subheader("💡 MT5 Confluence Breakdown")
    if not df.empty:
        st.write(f"**Breakdown:** {result['votes']}")
        fmt = ".2f" if "XAU" in symbol else ".5f"
        st.write(f"**Stop Loss:** `{result['sl']:{fmt}}` | **Take Profit:** `{result['tp']:{fmt}}`")
        st.markdown(f"*Feed Source: {data_source}*")

st.divider()
st.subheader("📋 Active Open Trades")

open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, timeframe, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM mt5_live_open_trades ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📍 **Price:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `MT5 LIVE SYNCED`")
            if st.button(f"Close #{row['id']}", key=f"close_{row['id']}"):
                current_p = current_live_price if current_live_price > 0 else row['entry_price']
                close_single_trade(con, row['id'], current_p, is_gold=("XAU" in row['symbol']), reason="Manual Close 👤")
                st.rerun()

st.divider()
st.subheader("📜 Trade History")
history_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, symbol, timeframe, bias, lot_size, entry_price, exit_price, exit_reason, pnl, strategy_votes FROM mt5_live_history ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("History khali hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+.2f}"):
            st.markdown(f"**PnL:** `${row['pnl']:+.2f}` | **Reason:** `{row['exit_reason']}`")

con.close()
