import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import requests

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BIGMAN — Scratch Built SMC Terminal", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- PROFESSIONAL INSTITUTIONAL STYLING ---
st.markdown("""
    <style>
    .main { background-color: #0b0f19; color: #f3f4f6; font-family: 'Inter', sans-serif; }
    .stApp { background: #0b0f19; }
    h1, h2, h3 { font-family: 'Inter', sans-serif; font-weight: 700; color: #ffffff; }
    .bigman-header { font-size: 2.4rem; font-weight: 800; background: linear-gradient(90deg, #3b82f6, #10b981); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0px; }
    .bigman-sub { font-size: 1rem; color: #9ca3af; margin-bottom: 25px; font-weight: 500; }
    .stButton>button { width: 100%; border-radius: 8px; font-weight: 600; background-color: #2563eb; color: white; border: none; padding: 10px; transition: all 0.3s ease; }
    .stButton>button:hover { background-color: #1d4ed8; box-shadow: 0 0 15px rgba(37, 99, 235, 0.4); }
    </style>
""", unsafe_allow_html=True)

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_scratch_v51.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"td": "XAU/USD", "yf": "GC=F", "mt5": "XAUUSD"},
    "EUR/USD": {"td": "EUR/USD", "yf": "EURUSD=X", "mt5": "EURUSD"},
    "GBP/USD": {"td": "GBP/USD", "yf": "GBPUSD=X", "mt5": "GBPUSD"},
}
INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m"},
    "1 hour": {"td": "1h", "yf": "60m"},
    "4 hours": {"td": "240min", "yf": "1h"},
    "1 day": {"td": "1day", "yf": "1d"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS open_trades_v51 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.01,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT,
            mt5_ticket INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history_v51 (
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
    if not cursor.fetchone():
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 3000.0)")
    con.commit()
    return con

def get_market_status():
    now_utc = datetime.now(timezone.utc)
    wd, hr = now_utc.weekday(), now_utc.hour
    if wd == 5 or (wd == 6 and hr < 22) or (wd == 4 and hr >= 22):
        return "CLOSED", "🔴 Market Closed (Weekend)"
    return "OPEN", "🟢 International Forex/Gold Market Live"

@st.cache_data(ttl=3, show_spinner=False)
def fetch_market_candles(symbol_label, interval_label, rows=150):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    
    try:
        import yfinance as yf
        df = yf.Ticker(info["yf"]).history(period="5d", interval=cfg["yf"])
        if not df.empty:
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df["datetime_ist"] = df.index.tz_convert(IST)
            live_p = float(df.close.iloc[-1])
            if live_p > 0:
                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "Yahoo Finance Feed 🟢"
    except Exception:
        pass

    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", "demo"))
    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": info["td"], "interval": cfg["td"], "outputsize": rows, "apikey": td_key},
            timeout=6,
        )
        payload = resp.json()
        if "values" in payload:
            df = pd.DataFrame(payload["values"])
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
            df["datetime_ist"] = df["datetime"].dt.tz_convert(IST)
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna().sort_values("datetime").set_index("datetime")
            live_p = float(df.close.iloc[-1])
            if live_p > 0:
                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "TwelveData Feed 🟢"
    except Exception:
        pass
        
    return pd.DataFrame(), "No Data Feed 🔴"

def evaluate_price_lesson_strategies(df):
    if len(df) < 25:
        return {"bias": "WAIT", "votes": "Insufficient data", "bullish": 0, "bearish": 0, "entry": 0, "sl": 0, "tp": 0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df.high.tail(40).max())
    swing_low = float(df.low.tail(40).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bull_votes, bear_votes = [], []

    if last.close < eq_50:
        bull_votes.append("Discount Zone (<50% Eq) [Buy Bias]")
    else:
        bear_votes.append("Premium Zone (>50% Eq) [Sell Bias]")

    if last.low > p_prev.high and prev.close > prev.open:
        bull_votes.append("Unmitigated Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bear_votes.append("Unmitigated Bearish FVG")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bull_votes.append("Demand Zone Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bear_votes.append("Supply Zone Wick Rejection")

    if last.close > last.open and prev.close < prev.open and last.open <= prev.close:
        bull_votes.append("Bullish Engulfing Pattern")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close:
        bear_votes.append("Bearish Engulfing Pattern")

    ma_fast = df.close.rolling(8).mean().iloc[-1]
    ma_slow = df.close.rolling(21).mean().iloc[-1]
    if ma_fast >= ma_slow:
        bull_votes.append("MA Bullish Trend")
    else:
        bear_votes.append("MA Bearish Trend")

    b_score = len(bull_votes)
    s_score = len(bear_votes)
    bias = "BUY" if b_score >= s_score else "SELL"

    current_close = float(last.close)
    is_gold = current_close > 100
    
    sl_buf = max(rng * 2.0, 6.0 if is_gold else 0.0030)
    tp_buf = sl_buf * 3.0

    if bias == "BUY":
        sl, tp = current_close - sl_buf, current_close + tp_buf
    else:
        sl, tp = current_close + sl_buf, current_close - tp_buf

    votes_summary = f"Bullish [{b_score}]: {', '.join(bull_votes)} | Bearish [{s_score}]: {', '.join(bear_votes)}"

    return {
        "bias": bias,
        "votes": votes_summary,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_close, 2 if is_gold else 5),
        "sl": round(sl, 2 if is_gold else 5),
        "tp": round(tp, 2 if is_gold else 5)
    }

def send_mt5_order(symbol_key, bias, lot_size, sl, tp):
    if not MT5_AVAILABLE:
        return False, 0, "MetaTrader5 library not available."
    if not mt5.initialize():
        return False, 0, "MT5 Initialization Failed."
        
    mt5_sym = SYMBOLS[symbol_key]["mt5"]
    info = mt5.symbol_info(mt5_sym)
    if not info.visible:
        mt5.symbol_select(mt5_sym, True)

    order_type = mt5.ORDER_TYPE_BUY if bias == "BUY" else mt5.ORDER_TYPE_SELL
    price = mt5.symbol_info_tick(mt5_sym).ask if bias == "BUY" else mt5.symbol_info_tick(mt5_sym).bid

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_sym,
        "volume": float(lot_size),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 20,
        "magic": 999888,
        "comment": "BIGMAN Scratch Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return False, 0, f"MT5 Error: {res.retcode}"
    return True, res.order, f"MT5 Ticket #{res.order}"

def get_balance(con):
    cur = con.cursor()
    cur.execute("SELECT balance FROM account_balance WHERE id = 1")
    return float(cur.fetchone()[0])

def update_bal(con, delta):
    cur_bal = get_balance(con)
    new_bal = cur_bal + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

# --- STREAMLIT UI ---
st.markdown('<p class="bigman-header">BIGMAN ⚡ Institutional Terminal</p>', unsafe_allow_html=True)
st.markdown('<p class="bigman-sub">Scratch-Built Price Lesson SMC Engine (Lightning Fast Mobile Native UI)</p>', unsafe_allow_html=True)

con = connect_db()
m_status, m_text = get_market_status()

with st.sidebar:
    st.header("🎛️ Control Panel")
    if st.button("🔄 Refresh Data & Prices", use_container_width=True):
        st.cache_data.clear()
        st.success("Refreshed!")
        st.rerun()

    st.markdown("---")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=0)
    lot = st.number_input("Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    st.markdown("---")
    exec_env = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    weekend_mode = st.checkbox("Allow Weekend Practice", value=True)
    auto_pilot = st.toggle("Enable Auto-Pilot Bot", value=False)
    
    if st.button("➕ Manual Order Execute", use_container_width=True):
        df_t, _ = fetch_market_candles(symbol, timeframe)
        if not df_t.empty:
            res_t = evaluate_price_lesson_strategies(df_t)
            ts = df_t.index[-1]
            e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
            
            cur = con.cursor()
            cur.execute("SELECT id FROM open_trades_v51 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(ts)))
            if cur.fetchone():
                st.sidebar.warning("Is candle par trade already open hai!")
            else:
                ticket = 0
                if e_mode == "MT5 LIVE":
                    ok, tkt, msg = send_mt5_order(symbol, res_t["bias"], lot, res_t["sl"], res_t["tp"])
                    if not ok:
                        st.sidebar.error(msg)
                        st.stop()
                    ticket = tkt
                
                ist_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
                con.execute(
                    "INSERT INTO open_trades_v51 (created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ist_time, symbol, timeframe, res_t["entry"], res_t["sl"], res_t["tp"], lot, res_t["bias"], res_t["votes"], str(ts), ticket)
                )
                con.commit()
                st.sidebar.success("Manual Order Executed Successfully!")
                st.rerun()

    st.markdown("---")
    if st.button("🔄 Reset Balance ($3,000)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 3000.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")
df, data_src = fetch_market_candles(symbol, timeframe)

if not df.empty:
    res = evaluate_price_lesson_strategies(df)
    live_p = float(df.close.iloc[-1])
    is_gold = "XAU" in symbol
    ts_now = df.index[-1]
    
    if auto_pilot and (m_status == "OPEN" or weekend_mode):
        cur = con.cursor()
        cur.execute("SELECT id FROM open_trades_v51 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(ts_now)))
        if not cur.fetchone():
            e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
            tkt = 0
            if e_mode == "MT5 LIVE":
                ok, tkt_id, _ = send_mt5_order(symbol, res["bias"], lot, res["sl"], res["tp"])
                if ok: tkt = tkt_id
            
            ist_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
            con.execute(
                "INSERT INTO open_trades_v51 (created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ist_time, symbol, timeframe, res["entry"], res["sl"], res["tp"], lot, res["bias"], res["votes"], str(ts_now), tkt)
            )
            con.commit()

    balance = get_balance(con)
    bal_color = "🟢" if balance >= 3000 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${balance:,.2f} USD** {bal_color}")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Live Price Feed", f"{live_p:.2f}" if is_gold else f"{live_p:.5f}")
    c2.metric("SMC Strategy Bias", res["bias"])
    c3.metric("Confluence Score", f"Bull: {res['bullish']} | Bear: {res['bearish']}")

st.divider()

col_left, col_right = st.columns([1.5, 1])

with col_left:
    st.subheader(f"📊 Recent Price Action ({symbol} - {timeframe})")
    st.caption(f"Data Source: {data_src}")
    
    display_df = df[["open", "high", "low", "close", "datetime_ist"]].tail(10).copy()
    display_df.columns = ["Open", "High", "Low", "Close", "Time (IST)"]
    st.dataframe(display_df, use_container_width=True)

with col_right:
    st.subheader("💡 Expert SMC Confluences")
    if not df.empty:
        st.write(f"**Strategy Breakdown:** {res['votes']}")
        fmt = ".2f" if is_gold else ".5f"
        st.write(f"**Stop Loss:** `{res['sl']:{fmt}}` | **Take Profit:** `{res['tp']:{fmt}}`")
        st.markdown(f"[🔗 Open Direct Chart on TradingView](https://www.tradingview.com/chart/?symbol=OANDA:{SYMBOLS[symbol]['td'].replace('/', '')})")

st.divider()
st.subheader("📋 Active Open Trades")

open_trades = pd.read_sql_query("SELECT * FROM open_trades_v51 ORDER BY id DESC", con)
if open_trades.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for _, row in open_trades.iterrows():
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        t_info = f" | Ticket: #{row['mt5_ticket']}" if row['mt5_ticket'] > 0 else ""
        with st.expander(f"📌 Trade #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot){t_info}"):
            sc1, sc2 = st.columns(2)
            sc1.markdown(f"🕒 **Time:** `{row['created_at_ist']}`\n📍 **Entry:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
            sc2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n📊 **Votes:** `{row['strategy_votes']}`")
            
            if st.button(f"Close Trade #{row['id']}", key=f"close_{row['id']}"):
                exit_p = live_p if not df.empty else row['entry_price']
                diff = (exit_p - row['entry_price']) if row['bias'] == "BUY" else (row['entry_price'] - exit_p)
                mult = 100 if "XAU" in row['symbol'] else 10000
                pnl = round(diff * row['lot_size'] * mult, 2)
                closed_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
                
                con.execute(
                    "INSERT INTO trade_history_v51 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row['id'], row['created_at_ist'], closed_ist, row['symbol'], row['timeframe'], row['entry_price'], round(exit_p, 2 if is_gold else 5), row['stop_loss'], row['take_profit'], row['lot_size'], row['bias'], "Manual Close", pnl, row['strategy_votes'])
                )
                con.execute("DELETE FROM open_trades_v51 WHERE id = ?", (row['id'],))
                con.commit()
                update_bal(con, pnl)
                st.toast(f"Trade closed! PnL: ${pnl:+.2f}")
                st.rerun()

con.close()
