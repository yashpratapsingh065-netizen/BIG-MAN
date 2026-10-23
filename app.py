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

st.set_page_config(page_title="BIG MAN2 | Auto-Trading Expert Bot", page_icon="🤖", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_price_action.db")
SYMBOLS = {
    "EUR/USD": {"tv": "FX:EURUSD", "yf": "EURUSD=X", "td": "EUR/USD"},
    "GBP/USD": {"tv": "FX:GBPUSD", "yf": "GBPUSD=X", "td": "GBP/USD"},
    "USD/JPY": {"tv": "FX:USDJPY", "yf": "JPY=X", "td": "USD/JPY"},
    "AUD/USD": {"tv": "FX:AUDUSD", "yf": "AUDUSD=X", "td": "AUD/USD"},
    "USD/CAD": {"tv": "FX:USDCAD", "yf": "CAD=X", "td": "USD/CAD"},
    "USD/CHF": {"tv": "FX:USDCHF", "yf": "CHF=X", "td": "USD/CHF"},
}
INTERVALS = {"15 min": ("15min", "15m"), "1 hour": ("1h", "1h"), "4 hours": ("4h", "1h"), "1 day": ("1day", "1d")}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS auto_trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            status TEXT NOT NULL,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            exit_price REAL,
            pnl REAL
        )
    """)
    con.commit()
    return con

def get_data(symbol_label, interval_label, rows=250):
    info = SYMBOLS[symbol_label]
    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", ""))
    td_interval, yf_interval = INTERVALS[interval_label]
    if td_key:
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": info["td"], "interval": td_interval, "outputsize": rows, "apikey": td_key},
                timeout=15,
            )
            payload = resp.json()
            if "values" in payload:
                df = pd.DataFrame(payload["values"])
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True).dt.tz_convert(IST)
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
                df = df.sort_values("datetime").set_index("datetime")
                return df[["open", "high", "low", "close"]].tail(rows), "Twelve Data"
        except Exception:
            pass
    try:
        period = "60d" if yf_interval in ("15m", "1h") else "2y"
        df = yf.download(info["yf"], period=period, interval=yf_interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame(), "No data"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        df = df[["open", "high", "low", "close"]].dropna()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
        if interval_label == "4 hours":
            df = df.resample("4h").agg({"open":"first", "high":"max", "low":"min", "close":"last"}).dropna()
        return df.tail(rows), "Yahoo Finance fallback"
    except Exception:
        return pd.DataFrame(), "No data"

def evaluate_8_strategies_voting(df):
    if len(df) < 30:
        return {"bias":"WAIT", "votes":"Kam data hai", "bullish":0, "bearish":0, "entry":0, "sl":0, "tp":0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    
    swing_high = float(df.high.tail(50).max())
    swing_low = float(df.low.tail(50).min())
    eq_level = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    # 1. FVG
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG")

    # 2. Premium / Discount Zone
    if last.close < eq_level:
        bullish_votes.append("Discount Zone (Buy Area)")
    else:
        bearish_votes.append("Premium Zone (Sell Area)")

    # 3. Engulfing
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        bearish_votes.append("Bearish Engulfing")

    # 4. Pin Bar
    if lower_wick >= body * 2 and lower_wick >= rng * 0.45 and last.close > last.open:
        bullish_votes.append("Bullish Pin Bar")
    elif upper_wick >= body * 2 and upper_wick >= rng * 0.45 and last.close < last.open:
        bearish_votes.append("Bearish Pin Bar")

    # 5. Breakout
    if last.close > prior_high:
        bullish_votes.append("High Breakout")
    elif last.close < prior_low:
        bearish_votes.append("Low Breakout")

    # 6. Momentum
    if body > rng * 0.7:
        if last.close > last.open:
            bullish_votes.append("Strong Momentum Up")
        else:
            bearish_votes.append("Strong Momentum Down")

    # 7. SMA Trend
    sma20 = float(df.close.tail(20).mean())
    sma50 = float(df.close.tail(50).mean()) if len(df) >= 50 else sma20
    if sma20 > sma50:
        bullish_votes.append("Uptrend SMA")
    else:
        bearish_votes.append("Downtrend SMA")

    # 8. Inside Bar
    if last.high < prev.high and last.low > prev.low:
        bullish_votes.append("Inside Bar Compression")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score > s_score and b_score >= 3:
        bias = "BUY"
    elif s_score > b_score and s_score >= 3:
        bias = "SELL"
    else:
        bias = "WAIT"

    current_close = float(last.close)
    if bias == "BUY":
        sl = current_close - (rng * 1.5)
        tp = current_close + (rng * 3.0)
    elif bias == "SELL":
        sl = current_close + (rng * 1.5)
        tp = current_close - (rng * 3.0)
    else:
        sl = current_close - (rng * 1.5)
        tp = current_close + (rng * 1.5)

    all_votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes)} | Bearish [{s_score}]: {', '.join(bearish_votes)}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": current_close,
        "sl": sl,
        "tp": tp
    }

def execute_auto_trade(con, symbol, timeframe, result):
    cursor = con.cursor()
    cursor.execute("SELECT id FROM auto_trade_journal WHERE symbol = ? AND status = 'OPEN'", (symbol,))
    existing = cursor.fetchone()
    
    if not existing and result["bias"] in ("BUY", "SELL"):
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        con.execute(
            "INSERT INTO auto_trade_journal(created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, status, bias, strategy_votes, exit_price, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe, result["entry"], result["sl"], result["tp"], "OPEN", result["bias"], result["votes"], 0.0, 0.0)
        )
        con.commit()

def close_open_trades(con, symbol, current_price):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, entry_price FROM auto_trade_journal WHERE symbol = ? AND status = 'OPEN'", (symbol,))
    open_trades = cursor.fetchall()
    
    for tid, bias, entry in open_trades:
        if bias == "BUY":
            pnl = (current_price - entry) * 10000
        else:
            pnl = (entry - current_price) * 10000
            
        con.execute(
            "UPDATE auto_trade_journal SET status = 'CLOSED', exit_price = ?, pnl = ? WHERE id = ?",
            (current_price, round(pnl, 2), tid)
        )
        con.commit()

# --- UI DESIGN ---
st.title("🤖 BIG MAN2 — Auto-Trading Expert Bot")
st.caption("8 Strategies Voting Engine + Fully Automated Trade Execution + Market Refresh + Live Journal")

with st.sidebar:
    st.header("⚙️ Bot Control Panel")
    symbol = st.selectbox("Currency Pair", list(SYMBOLS.keys()))
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    # MARKET REFRESH BUTTON
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.toast("Market data refreshed successfully!", icon="🔄")
    
    st.markdown("---")
    st.subheader("⚡ Auto-Pilot Mode")
    auto_mode = st.toggle("Enable Auto-Trading Bot", value=False)
    if auto_mode:
        st.success("🟢 Bot is RUNNING autonomously based on strategy voting!")
    else:
        st.warning("🟡 Bot is in MANUAL / MONITOR mode.")
        
    st.markdown("---")
    if st.button("🔴 Close All Open Trades for Pair", use_container_width=True):
        con_temp = connect_db()
        df_temp, _ = get_data(symbol, timeframe)
        if not df_temp.empty:
            close_open_trades(con_temp, symbol, float(df_temp.close.iloc[-1]))
            st.sidebar.success("Open trades closed successfully!")
        con_temp.close()

con = connect_db()
df, source = get_data(symbol, timeframe)

if not df.empty:
    result = evaluate_8_strategies_voting(df)
    
    if auto_mode:
        execute_auto_trade(con, symbol, timeframe, result)

    st.markdown("### 📊 Expert Voting & Strategy Dashboard")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Live Price", f"{result['entry']:.5f}")
    col2.metric("Bullish Votes", f"{result['bullish']} / 8")
    col3.metric("Bearish Votes", f"{result['bearish']} / 8")
    
    if result["bias"] == "BUY":
        col4.success("🟢 FINAL DECISION: BUY")
    elif result["bias"] == "SELL":
        col4.error("🔴 FINAL DECISION: SELL")
    else:
        col4.warning("🟡 FINAL DECISION: WAIT")

    st.info(f"**Strategy Breakdown:** {result['votes']}")
    
    c_l1, c_l2 = st.columns(2)
    c_l1.metric("Calculated Stop Loss (SL)", f"{result['sl']:.5f}")
    c_l2.metric("Calculated Take Profit (TP)", f"{result['tp']:.5f}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol})")
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
    st.subheader("💡 Recent Candles & Status")
    if df.empty:
        st.error("Data load nahi ho pa raha hai.")
    else:
        st.write("**Pichli 4 Candles (IST):**")
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()

st.subheader("📋 Bot Trade Journal & Execution History (IST Time)")
try:
    logs = pd.read_sql_query(
        "SELECT created_at_ist, symbol, timeframe, status, bias, entry_price, stop_loss, take_profit, exit_price, pnl, strategy_votes FROM auto_trade_journal ORDER BY id DESC LIMIT 50",
        con,
    )
    if logs.empty:
        st.info("Abhi koi auto-trade execute nahi hua hai. Sidebar se 'Enable Auto-Trading Bot' ON karein.")
    else:
        logs.columns = ['Time', 'Pair', 'TF', 'Status', 'Bias', 'Entry', 'SL', 'TP', 'Exit Price', 'PnL (Pips)', 'Votes Info']
        st.dataframe(logs, use_container_width=True)
except Exception:
    st.info("Journal database update ho rahi hai.")

con.close()
