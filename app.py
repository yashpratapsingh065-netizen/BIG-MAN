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

st.set_page_config(page_title="BIG MAN2 | Price Action & SMC Bot", page_icon="📈", layout="wide")

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
        CREATE TABLE IF NOT EXISTS price_action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            candle_time_ist TEXT,
            close REAL,
            pattern TEXT NOT NULL,
            bias TEXT NOT NULL,
            details TEXT
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

def analyze_price_action(df):
    if len(df) < 25:
        return {"bias":"WAIT", "pattern":"Kam candles hain", "details":"Kam se kam 25 candles chahiye analysis ke liye."}
    
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
    zone = "PREMIUM (Sell Zone)" if last.close > eq_level else "DISCOUNT (Buy Zone)"
    
    patterns = []
    bias = "WAIT"

    if last.low > p_prev.high and prev.close > prev.open:
        if last.close < eq_level:
            patterns.append("Bullish FVG (SMC Buy Signal)")
            bias = "BUY"
        else:
            patterns.append("Bullish FVG (Premium Zone)")
            
    elif last.high < p_prev.low and prev.close < prev.open:
        if last.close > eq_level:
            patterns.append("Bearish FVG (SMC Sell Signal)")
            bias = "SELL"
        else:
            patterns.append("Bearish FVG (Discount Zone)")

    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        patterns.append("Bullish Engulfing")
        if bias == "WAIT": bias = "BUY"
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        patterns.append("Bearish Engulfing")
        if bias == "WAIT": bias = "SELL"

    if lower_wick >= body * 2 and lower_wick >= rng * 0.45 and last.close > last.open:
        patterns.append("Bullish Pin Bar / Rejection")
        if bias == "WAIT": bias = "BUY"
    elif upper_wick >= body * 2 and upper_wick >= rng * 0.45 and last.close < last.open:
        patterns.append("Bearish Pin Bar / Rejection")
        if bias == "WAIT": bias = "SELL"

    if last.close > prior_high:
        patterns.append("High Breakout")
        if bias == "WAIT": bias = "BUY"
    elif last.close < prior_low:
        patterns.append("Low Breakout")
        if bias == "WAIT": bias = "SELL"

    if not patterns:
        patterns = ["Koi clear Signal nahi hai abhi"]

    details_msg = f"Current Market Zone: **{zone}** (Equilibrium: {eq_level:.5f}). High: {swing_high:.5f}, Low: {swing_low:.5f}."

    return {
        "bias": bias,
        "pattern": ", ".join(patterns),
        "details": details_msg,
    }

def log_analysis(con, symbol, timeframe, df, result):
    candle_time_ist = df.index[-1].strftime('%Y-%m-%d %I:%M:%S %p IST') if not df.empty else ""
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    close = float(df.close.iloc[-1]) if not df.empty else None
    
    con.execute(
        "INSERT INTO price_action_log(created_at_ist,symbol,timeframe,candle_time_ist,close,pattern,bias,details) VALUES(?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, candle_time_ist, close,
         result["pattern"], result["bias"], result["details"])
    )
    con.commit()

# --- STREAMLIT UI ---
st.title("⚡ Price Action & SMC Trading Bot")
st.caption("Price Lesson Strategies: Supply & Demand, Fair Value Gap (FVG), Premium & Discount Zones.")

with st.sidebar:
    st.header("⚙️ Controls")
    symbol = st.selectbox("Pair Select Karein", list(SYMBOLS.keys()))
    timeframe = st.selectbox("Timeframe Select Karein", list(INTERVALS.keys()), index=1)
    run_analysis = st.button("🔍 Scan / Generate Signal", type="primary", use_container_width=True)

con = connect_db()
df, source = get_data(symbol, timeframe)

if not df.empty:
    result = analyze_price_action(df)
    col1, col2, col3 = st.columns(3)
    col1.metric("Live Rate", f"{float(df.close.iloc[-1]):.5f}")
    
    if result["bias"] == "BUY":
        col2.success("🟢 BUY SIGNAL")
    elif result["bias"] == "SELL":
        col2.error("🔴 SELL SIGNAL")
    else:
        col2.warning("🟡 WAIT (No Clear Trade)")
        
    col3.markdown(f"**Detected Strategy:**\n{result['pattern']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📊 Live Chart ({symbol} - {timeframe})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_interval = {"15 min":"15", "1 hour":"60", "4 hours":"240", "1 day":"D"}[timeframe]
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
    st.subheader("💡 Analysis Details")
    if df.empty:
        st.error("Market Data load nahi ho pa raha hai.")
    else:
        st.write(result["details"])
        st.markdown("---")
        st.write("**Recent Candles (IST Time):**")
        view = df.tail(6).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)
        
        if run_analysis:
            log_analysis(con, symbol, timeframe, df, result)
            st.success("Signal Saved to Local History!")

st.divider()

st.subheader("📋 Trade Signal History (IST Time)")
logs = pd.read_sql_query(
    "SELECT created_at_ist as 'Created At', symbol as 'Pair', timeframe as 'TF', candle_time_ist as 'Candle Time', close as 'Close Rate', pattern as 'Strategy Pattern', bias as 'Signal', details as 'Zone Info' FROM price_action_log ORDER BY id DESC LIMIT 50",
    con,
)
if logs.empty:
    st.info("Abhi tak koi signal save nahi hua hai. 'Scan / Generate Signal' button dabayein.")
else:
    st.dataframe(logs, use_container_width=True)

con.close()
