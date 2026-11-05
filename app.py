import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BIGMAN — Institutional Trading Terminal", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- MODERN ULTRA-CLEAN CSS STYLING ---
st.markdown("""
    <style>
    .main { background-color: #0e1117; color: #f0f2f6; font-family: 'Inter', sans-serif; }
    .stApp { background: #0e1117; }
    h1, h2, h3 { font-family: 'Inter', sans-serif; font-weight: 700; letter-spacing: -0.5px; color: #ffffff; }
    .bigman-header { font-size: 2.2rem; font-weight: 800; background: linear-gradient(90deg, #3b82f6, #10b981); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0px; }
    .bigman-sub { font-size: 0.95rem; color: #9ca3af; margin-bottom: 20px; font-weight: 500; }
    .metric-card { background: #1f2937; border: 1px solid #374151; padding: 16px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
    .stButton>button { width: 100%; border-radius: 8px; font-weight: 600; background-color: #2563eb; color: white; border: none; transition: all 0.3s ease; }
    .stButton>button:hover { background-color: #1d4ed8; box-shadow: 0 0 15px rgba(37, 99, 235, 0.4); }
    </style>
""", unsafe_allow_html=True)

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_institutional_v47.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F", "mt5": "XAUUSD"},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X", "mt5": "EURUSD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X", "mt5": "GBPUSD"},
}
INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "minutes": 60},
    "4 hours": {"td": "4h", "yf": "1h", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v47 (
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
            candle_timestamp TEXT,
            mt5_order_ticket INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history_v47 (
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

@st.cache_data(ttl=2, show_spinner=False)
def get_tradingview_matched_data(symbol_label, interval_label, rows=250):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    
    try:
        import yfinance as yf
        ticker = yf.Ticker(info["yf"])
        df_yf = ticker.history(period="5d", interval=cfg["yf"])
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df_yf["datetime_ist"] = df_yf.index.tz_convert(IST)
            live_price = float(df_yf.close.iloc[-1])
            if live_price > 0:
                return df_yf[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "TradingView Live Spot Feed 🟢"
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
            live_price = float(df.close.iloc[-1])
            if live_price > 0:
                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "TwelveData Direct 🟢"
    except Exception:
        pass
        
    return pd.DataFrame(), "No Data 🔴"

def evaluate_price_lesson_strategies(df):
    if len(df) < 30:
        return {"bias":"WAIT", "votes":"Insufficient data for analysis", "bullish":0, "bearish":0, "entry":0, "sl":0, "tp":0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    # Price Lesson S7: Premium & Discount Zones (50% Equilibrium Model)
    swing_high = float(df.high.tail(50).max())
    swing_low = float(df.low.tail(50).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    if last.close < eq_50:
        bullish_votes.append("Discount Zone (<50% Eq) -> Cheap Buy Area")
    else:
        bearish_votes.append("Premium Zone (>50% Eq) -> Expensive Sell Area")

    # Price Lesson S5-FVG: Unmitigated Fair Value Gap Imbalance
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Unmitigated Bullish FVG Imbalance")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Unmitigated Bearish FVG Imbalance")

    # Price Lesson S6: Supply & Demand Wick Rejections
    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Zone Strong Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Zone Strong Wick Rejection")

    # Price Lesson S3: Candlestick Engulfing Patterns
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing Confirmation")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        bearish_votes.append("Bearish Engulfing Confirmation")

    # Multi-Timeframe / Breakout Confluence
    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    if last.close > prior_high:
        bullish_votes.append("Resistance High Breakout")
    elif last.close < prior_low:
        bearish_votes.append("Support Low Breakdown")

    # Momentum Confluence via Moving Average
    ma_fast = df.close.rolling(8).mean().iloc[-1]
    ma_slow = df.close.rolling(21).mean().iloc[-1]
    if ma_fast >= ma_slow:
        bullish_votes.append("MA Fast >= MA Slow (Bullish Trend)")
    else:
        bearish_votes.append("MA Fast < MA Slow (Bearish Trend)")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score >= s_score:
        bias = "BUY"
    else:
        bias = "SELL"

    current_close = float(last.close)
    is_gold = current_close > 100
    
    # Optimized Risk-Reward Ratio (1:3 RRR for gross profit protection)
    sl_buffer = max(rng * 2.0, 6.0 if is_gold else 0.0030)
    tp_buffer = sl_buffer * 3.0

    if bias == "BUY":
        sl = current_close - sl_buffer
        tp = current_close + tp_buffer
    else:
        sl = current_close + sl_buffer
        tp = current_close - tp_buffer

    all_votes_str = f"Bullish Confluence [{b_score}]: {', '.join(bullish_votes)} | Bearish Confluence [{s_score}]: {', '.join(bearish_votes)}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_close, 2 if is_gold else 5),
        "sl": round(sl, 2 if is_gold else 5),
        "tp": round(tp, 2 if is_gold else 5)
    }

def send_mt5_order(symbol_key, bias, lot_size, sl, tp):
    if not MT5_AVAILABLE:
        return False, 0, "MetaTrader5 library is not installed on system."
        
    if not mt5.initialize():
        return False, 0, "MT5 Terminal Connection Failed! Open MT5 App."
        
    mt5_sym = SYMBOLS[symbol_key]["mt5"]
    symbol_info = mt5.symbol_info(mt5_sym)
    if symbol_info is None:
        return False, 0, f"{mt5_sym} Symbol not found in MT5 Market Watch."

    if not symbol_info.visible:
        mt5.symbol_select(mt5_sym, True)

    order_type = mt5.ORDER_TYPE_BUY if bias == "BUY" else mt5.ORDER_TYPE_SELL
    price = mt5.symbol_info_tick(mt5_sym).ask if bias == "BUY" else mt5.symbol_info_tick(mt5_sym).bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_sym,
        "volume": float(lot_size),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 20,
        "magic": 999888,
        "comment": "BIGMAN Institutional Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, 0, f"MT5 Error Code: {result.retcode}"
    
    return True, result.order, f"MT5 Execution Successful (Ticket: #{result.order})"

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

def execute_auto_trade(con, symbol, timeframe, result, lot_size, candle_ts, execution_mode="PAPER", market_status="OPEN", allow_weekend_practice=True):
    if market_status != "OPEN" and not allow_weekend_practice:
        return False, "Market Closed! Enable Weekend Practice Mode."
        
    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v47 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(candle_ts)))
    if cursor.fetchone():
        return False, "Is candle par pehle hi trade li ja chuki hai!"

    mt5_ticket = 0
    if execution_mode == "MT5 LIVE":
        success, ticket, msg = send_mt5_order(symbol, result["bias"], lot_size, result["sl"], result["tp"])
        if not success:
            return False, f"MT5 Order Failed: {msg}"
        mt5_ticket = ticket

    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO tv_open_trades_v47 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_order_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts), mt5_ticket)
    )
    con.commit()
    return True, f"Trade opened successfully! ({execution_mode})"

def close_single_trade(con, trade_id, current_price, is_gold=True, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, mt5_order_ticket FROM tv_open_trades_v47 WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes, mt5_ticket = row
    
    if bias == "BUY":
        diff = current_price - entry_price
    else:
        diff = entry_price - current_price
        
    multiplier = 100 if is_gold else 10000
    pnl_usd = round(diff * lot_size * multiplier, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO trade_history_v47 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if is_gold else 5), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v47 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

# --- MODERN UI HEADER ---
st.markdown('<p class="bigman-header">BIGMAN ⚡ Institutional Terminal</p>', unsafe_allow_html=True)
st.markdown('<p class="bigman-sub">Advanced Price Lesson SMC Confluence Engine & Multi-Broker Execution</p>', unsafe_allow_html=True)

con = connect_db()
m_status, m_text = get_forex_market_status()

# --- SIDEBAR CONTROL PANEL ---
with st.sidebar:
    st.header("🎛️ Control Panel")
    
    # 🔄 Market Refresh Button Added Here
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.cache_data.clear()
        st.success("Market feeds refreshed successfully!")
        st.rerun()

    st.markdown("---")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=0)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    st.markdown("---")
    st.subheader("🛠️ Execution Setup")
    exec_env = st.radio("Broker Environment", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    
    weekend_practice = st.checkbox("Allow Weekend Practice Mode", value=True)
    auto_mode = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Execute Manual Order", use_container_width=True):
        df_temp, _ = get_tradingview_matched_data(symbol, timeframe)
        if not df_temp.empty:
            res_temp = evaluate_price_lesson_strategies(df_temp)
            candle_ts = df_temp.index[-1]
            e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
            success, msg = execute_auto_trade(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts, execution_mode=e_mode, market_status=m_status, allow_weekend_practice=weekend_practice)
            if success:
                st.sidebar.success(msg)
            else:
                st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🔄 Reset Demo Balance ($3,000)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 3000.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset to $3,000.00!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")
df, data_source = get_tradingview_matched_data(symbol, timeframe)

if not df.empty:
    result = evaluate_price_lesson_strategies(df)
    current_live_price = float(df.close.iloc[-1])
    is_gold = "XAU" in symbol
    current_candle_ts = df.index[-1]
    
    if auto_mode and (m_status == "OPEN" or weekend_practice):
        e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
        execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts, execution_mode=e_mode, market_status=m_status, allow_weekend_practice=weekend_practice)

    curr_balance = get_current_balance(con)
    bal_color = "🟢" if curr_balance >= 3000 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Live Chart Price", f"{current_live_price:.2f}" if is_gold else f"{current_live_price:.5f}")
    with col2:
        st.metric("Signal Bias", result["bias"])
    with col3:
        st.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Terminal ({symbol} - {timeframe})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_interval = {"15 min": "15", "1 hour": "60", "4 hours": "240", "1 day": "D"}[timeframe]
    html = f"""
    <div class="tradingview-widget-container" style="height:500px;width:100%">
      <div id="tradingview_chart" style="height:480px;width:100%"></div>
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
    components.html(html, height=510, scrolling=False)

with right:
    st.subheader("💡 Expert Confluence Breakdown")
    if not df.empty:
        st.write(f"**Strategy Breakdown:** {result['votes']}")
        fmt = ".2f" if "XAU" in symbol else ".5f"
        st.write(f"**Stop Loss:** `{result['sl']:{fmt}}` | **Take Profit:** `{result['tp']:{fmt}}`")
        st.markdown(f"*Feed Source: {data_source}*")

st.divider()

st.subheader("📋 Active Open Trades")
open_df = pd.read_sql_query("SELECT * FROM tv_open_trades_v47 ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        fmt = ".2f" if "XAU" in row['symbol'] else ".5f"
        t_ticket = f" | MT5 Ticket: #{row['mt5_order_ticket']}" if row['mt5_order_ticket'] > 0 else ""
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot){t_ticket}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n📍 **Entry Price:** `{row['entry_price']:{fmt}}`\n🛑 **Stop Loss:** `{row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **Take Profit:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            
            if st.button(f"Close Trade #{row['id']} Now", key=f"close_open_{row['id']}"):
                current_p = float(df.close.iloc[-1]) if not df.empty else row['entry_price']
                pnl_realized = close_single_trade(con, row['id'], current_p, is_gold=("XAU" in row['symbol']), reason="Manual Close 👤")
                st.toast(f"Trade #{row['id']} closed! PnL: ${pnl_realized:+.2f}")
                st.rerun()

con.close()
