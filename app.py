import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="MR. INDIE — Institutional Trading Platform",
    page_icon="🇮🇳",
    layout="wide"
)

DB_PATH = os.getenv("MRINDIE_DB_PATH", "mrindie_trading_bot.db")

INDIAN_SYMBOLS = {
    "NIFTY 50": {"yf": "^NSEI", "tv": "NSE:NIFTY", "lot": 25, "dhan_sec_id": "13"},
    "BANK NIFTY": {"yf": "^NSEBANK", "tv": "NSE:BANKNIFTY", "lot": 15, "dhan_sec_id": "25"},
    "RELIANCE": {"yf": "RELIANCE.NS", "tv": "NSE:RELIANCE", "lot": 1, "dhan_sec_id": "2885"},
    "TCS": {"yf": "TCS.NS", "tv": "NSE:TCS", "lot": 1, "dhan_sec_id": "11536"},
    "HDFC BANK": {"yf": "HDFCBANK.NS", "tv": "NSE:HDFCBANK", "lot": 1, "dhan_sec_id": "1333"}
}

INTERVALS = {
    "5 min": {"yf": "5m", "minutes": 5, "tv": "5"},
    "15 min": {"yf": "15m", "minutes": 15, "tv": "15"},
    "1 hour": {"yf": "60m", "minutes": 60, "tv": "60"},
    "1 day": {"yf": "1d", "minutes": 1440, "tv": "D"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mrindie_open_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS mrindie_trade_history (
            id INTEGER PRIMARY KEY,
            created_at_ist TEXT NOT NULL,
            closed_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            quantity INTEGER,
            bias TEXT NOT NULL,
            mode TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl_inr REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mrindie_account_balance (
            id INTEGER PRIMARY KEY,
            balance_inr REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance_inr FROM mrindie_account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO mrindie_account_balance (id, balance_inr) VALUES (1, 100.0)")
    con.commit()
    return con

def get_indian_market_status():
    now_ist = datetime.now(IST)
    current_time = now_ist.time()
    
    if now_ist.weekday() >= 5:
        return "WEEKEND_CLOSED", "🔴 Market Closed (Weekend)"
        
    m_open = datetime.strptime("09:15:00", "%H:%M:%S").time()
    m_close = datetime.strptime("15:30:00", "%H:%M:%S").time()
    sq_time = datetime.strptime("15:15:00", "%H:%M:%S").time()
    
    if sq_time <= current_time <= m_close:
        return "AUTO_SQUARE_OFF", "⚠️ Intraday Auto Square-off Window (3:15 PM+)"
    elif m_open <= current_time <= m_close:
        return "OPEN", "🟢 Market Live (NSE/BSE)"
    else:
        return "CLOSED", "🔴 Market Closed (Trading Hours: 9:15 AM - 3:30 PM IST)"

def fetch_synchronized_market_data(symbol_label, timeframe_label):
    info = INDIAN_SYMBOLS[symbol_label]
    cfg = INTERVALS[timeframe_label]
    try:
        ticker = yf.Ticker(info["yf"])
        df = ticker.history(period="5d", interval=cfg["yf"])
        if not df.empty:
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df["datetime_ist"] = df.index.tz_convert(IST)
            return df[["open", "high", "low", "close", "datetime_ist"]].tail(200), "NSE Synchronized Feed"
    except Exception:
        pass
    return pd.DataFrame(), "No Data"

def evaluate_strict_smc_strategy(df):
    if len(df) < 30:
        return {"bias": "WAIT", "votes": "Data insufficient", "bullish": 0, "bearish": 0, "entry": 0, "sl": 0, "tp": 0}
    
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
        bullish_votes.append("Discount Zone")
    else:
        bearish_votes.append("Premium Zone")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Unmitigated Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bullish_votes.append("Unmitigated Bearish FVG")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bullish_votes.append("Supply Wick Rejection")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score:
        bias = "BUY"
    elif s_score > b_score:
        bias = "SELL"
    else:
        bias = "WAIT"

    curr_close = float(last.close)
    sl_pts = max(rng * 1.5, 15.0 if curr_close > 1000 else 2.0)
    tp_pts = max(rng * 3.0, 30.0 if curr_close > 1000 else 4.0)

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

def get_account_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance_inr FROM mrindie_account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 100.0

def modify_balance(con, delta):
    new_balance = get_account_balance(con) + delta
    con.execute("UPDATE mrindie_account_balance SET balance_inr = ? WHERE id = 1", (new_balance,))
    con.commit()
    return new_balance

def execute_trade_safely(con, symbol, timeframe, analysis, qty, mode, candle_ts, market_code):
    # STRICT MARKET HOURS CHECK: Agar market live nahi hai toh trade execute nahi hogi!
    if market_code != "OPEN":
        return False, "Market is Closed! Trading restricted."

    if analysis["bias"] == "WAIT":
        return False, "Neutral Signal (WAIT)"

    cursor = con.cursor()
    cursor.execute("SELECT id FROM mrindie_open_trades WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(candle_ts)))
    if cursor.fetchone():
        return False, "Duplicate Candle Trade"

    created_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    con.execute(
        "INSERT INTO mrindie_open_trades (created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, quantity, bias, mode, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_time, symbol, timeframe, analysis["entry"], analysis["sl"], analysis["tp"], qty, analysis["bias"], mode, analysis["votes"], str(candle_ts))
    )
    con.commit()
    return True, "Success"

def close_trade_safely(con, trade_id, current_price, exit_reason="Manual Exit 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, quantity, bias, mode, strategy_votes FROM mrindie_open_trades WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0

    created_time, symbol, timeframe, entry_p, sl, tp, qty, bias, mode, votes = row
    price_diff = (current_price - entry_p) if bias == "BUY" else (entry_p - current_price)
    pnl = round(price_diff * qty, 2)
    closed_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')

    con.execute(
        "INSERT INTO mrindie_trade_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, quantity, bias, mode, exit_reason, pnl_inr, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_time, closed_time, symbol, timeframe, entry_p, round(current_price, 2), sl, tp, qty, bias, mode, exit_reason, pnl, votes)
    )
    con.execute("DELETE FROM mrindie_open_trades WHERE id = ?", (trade_id,))
    con.commit()
    modify_balance(con, pnl)
    return pnl

st.title("🇮🇳 MR. INDIE — Institutional Trading Platform")
st.caption("Advanced SMC Confluence Engine • Market Hours Guardrail • Exact Price Sync")

con = connect_db()
market_status_code, market_status_text = get_indian_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    selected_symbol = st.selectbox("Select Instrument", list(INDIAN_SYMBOLS.keys()), index=0)
    selected_tf = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = INDIAN_SYMBOLS[selected_symbol]["lot"]
    order_qty = st.number_input("Order Quantity / Lots", min_value=1, max_value=1000, value=default_lot, step=1)
    
    st.markdown("---")
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.toast("Market feeds & prices re-synchronized!", icon="🔄")
        st.rerun()

    st.markdown("---")
    st.subheader("🛡️ Execution Mode")
    exec_mode = st.radio("Mode Selection", ["DEMO (Paper Trading)", "LIVE BROKER (Dhan API)"], index=0)
    
    st.markdown("---")
    auto_pilot = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Execute Signal Trade", use_container_width=True):
        df_chk, _ = fetch_synchronized_market_data(selected_symbol, selected_tf)
        if not df_chk.empty:
            res_chk = evaluate_strict_smc_strategy(df_chk)
            c_ts = df_chk.index[-1]
            m_str = "LIVE" if "LIVE" in exec_mode else "DEMO"
            ok, msg = execute_trade_safely(con, selected_symbol, selected_tf, res_chk, order_qty, m_str, c_ts, market_status_code)
            if ok:
                st.sidebar.success(f"Trade placed for {selected_symbol}!")
            else:
                st.sidebar.error(f"Blocked: {msg}")

    st.markdown("---")
    if st.button("🔄 Reset Capital to ₹100", use_container_width=True):
        con.execute("UPDATE mrindie_account_balance SET balance_inr = 100.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Capital reset to ₹100!")
        st.rerun()

st.info(f"Market Status: **{market_status_text}**")
df_data, source_info = fetch_synchronized_market_data(selected_symbol, selected_tf)

if not df_data.empty:
    analysis_result = evaluate_strict_smc_strategy(df_data)
    current_price = float(df_data.close.iloc[-1])
    candle_timestamp = df_data.index[-1]
    mode_string = "LIVE" if "LIVE" in exec_mode else "DEMO"

    if auto_pilot and market_status_code == "OPEN":
        execute_trade_safely(con, selected_symbol, selected_tf, analysis_result, order_qty, mode_string, candle_timestamp, market_status_code)

    account_balance = get_account_balance(con)
    st.markdown(f"### 💼 Paper Capital: **₹{account_balance:,.2f} INR**")
    
    col1, col2, col3 = st.columns(3)
    col1.metric(f"Live Exchange Price ({selected_symbol})", f"₹{current_price:,.2f}")
    col2.metric("Signal Bias", analysis_result["bias"])
    col3.metric("Confluence Score", f"Bull: {analysis_result['bullish']} | Bear: {analysis_result['bearish']}")

st.divider()

layout_left, layout_right = st.columns([1.8, 1])

with layout_left:
    st.subheader(f"📈 Live Market Chart ({selected_symbol})")
    tv_symbol_code = INDIAN_SYMBOLS[selected_symbol]["tv"]
    tv_interval_code = INTERVALS[selected_tf]["tv"]
    
    tv_html_code = f"""
    <div class="tradingview-widget-container" style="height:480px;width:100%">
      <div id="tv_chart_container" style="height:460px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget({{
        "autosize": true,
        "symbol": "{tv_symbol_code}",
        "interval": "{tv_interval_code}",
        "timezone": "Asia/Kolkata",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "enable_publishing": false,
        "allow_symbol_change": false,
        "container_id": "tv_chart_container"
      }});
      </script>
    </div>
    """
    components.html(tv_html_code, height=490, scrolling=False)

with layout_right:
    st.subheader("🧠 Institutional Analysis Breakdown")
    if not df_data.empty:
        st.write(f"**Confluence Votes:** {analysis_result['votes']}")
        st.write(f"**Stop Loss:** `₹{analysis_result['sl']:,.2f}` | **Take Profit:** `₹{analysis_result['tp']:,.2f}`")
        st.caption(f"Data Feed Provider: {source_info} | Price Reference: Spot LTP")

st.divider()

st.subheader("📋 Active Open Positions")
open_df = pd.read_sql_query("SELECT * FROM mrindie_open_trades ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open position active nahi hai.")
else:
    for idx, row_item in open_df.iterrows():
        with st.expander(f"📌 Trade #{row_item['id']} | {row_item['symbol']} ({row_item['bias']} - {row_item['quantity']} Qty)"):
            c_a, c_b = st.columns(2)
            c_a.markdown(f"🕒 **Entry Time:** `{row_item['created_at_ist']}`\n📍 **Entry Price:** `₹{row_item['entry_price']:,.2f}`\n🛑 **Stop Loss:** `₹{row_item['stop_loss']:,.2f}`")
            c_b.markdown(f"🎯 **Take Profit:** `₹{row_item['take_profit']:,.2f}`\n⚡ **Timeframe:** `{row_item['timeframe']}`\n📊 **Votes:** `{row_item['strategy_votes']}`")
            
            if st.button(f"Square Off Position #{row_item['id']}", key=f"sq_pos_{row_item['id']}"):
                pnl_realized = close_trade_safely(con, row_item['id'], current_price, exit_reason="Manual Square-Off 👤")
                st.toast(f"Trade #{row_item['id']} closed! Realized PnL: ₹{pnl_realized:+,.2f}")
                st.rerun()

con.close()
