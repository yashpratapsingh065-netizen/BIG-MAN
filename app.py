import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import requests

st.set_page_config(page_title="BIG MAN2 | Price Action", page_icon="📈", layout="wide")

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
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            candle_time TEXT,
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
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
                df = df.sort_values("datetime").set_index("datetime")
                return df[["open", "high", "low", "close"]].tail(rows), "Twelve Data"
        except Exception:
            pass
    try:
        # Yahoo Finance does not support 4h directly; fetch 1h and resample.
        period = "60d" if yf_interval in ("15m", "1h") else "2y"
        df = yf.download(info["yf"], period=period, interval=yf_interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame(), "No data"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        df = df[["open", "high", "low", "close"]].dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        if interval_label == "4 hours":
            df = df.resample("4h").agg({"open":"first", "high":"max", "low":"min", "close":"last"}).dropna()
        return df.tail(rows), "Yahoo Finance fallback"
    except Exception:
        return pd.DataFrame(), "No data"

def analyze_price_action(df):
    if len(df) < 25:
        return {"bias":"WAIT", "pattern":"Not enough candles", "details":"At least 25 candles are needed."}
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    sma20 = float(df.close.tail(20).mean())
    sma50 = float(df.close.tail(50).mean()) if len(df) >= 50 else float(df.close.mean())

    patterns = []
    bias = "WAIT"

    # Bullish/bearish engulfing
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        patterns.append("Bullish engulfing")
        bias = "BUY"
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        patterns.append("Bearish engulfing")
        bias = "SELL"

    # Pin bar / rejection wick
    if lower_wick >= body * 2 and lower_wick >= rng * 0.45 and last.close > last.open:
        patterns.append("Bullish rejection / pin bar")
        if bias == "WAIT": bias = "BUY"
    elif upper_wick >= body * 2 and upper_wick >= rng * 0.45 and last.close < last.open:
        patterns.append("Bearish rejection / pin bar")
        if bias == "WAIT": bias = "SELL"

    # Close beyond prior 20-candle range
    if last.close > prior_high:
        patterns.append("20-candle breakout up")
        bias = "BUY"
    elif last.close < prior_low:
        patterns.append("20-candle breakout down")
        bias = "SELL"

    # Trend context
    if sma20 > sma50:
        trend = "Uptrend context (SMA20 > SMA50)"
    elif sma20 < sma50:
        trend = "Downtrend context (SMA20 < SMA50)"
    else:
        trend = "Flat trend context"

    # Inside bar
    if last.high < prev.high and last.low > prev.low:
        patterns.append("Inside bar (consolidation)")
        if bias == "WAIT": bias = "WAIT"

    if not patterns:
        patterns = ["No clear price-action pattern"]
    return {
        "bias": bias,
        "pattern": ", ".join(patterns),
        "details": f"{trend}. Prior 20-candle high: {prior_high:.5f}; low: {prior_low:.5f}.",
    }

def log_analysis(con, symbol, timeframe, df, result):
    candle_time = str(df.index[-1]) if not df.empty else ""
    close = float(df.close.iloc[-1]) if not df.empty else None
    con.execute(
        "INSERT INTO price_action_log(created_at,symbol,timeframe,candle_time,close,pattern,bias,details) VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), symbol, timeframe, candle_time, close,
         result["pattern"], result["bias"], result["details"])
    )
    con.commit()

st.title("📈 BIG MAN2 — Price Action Desk")
st.caption("TradingView chart + price-action analysis log. Signals are educational/demo-only, not guaranteed trade calls.")

with st.sidebar:
    st.header("Market settings")
    symbol = st.selectbox("Forex pair", list(SYMBOLS.keys()))
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    st.caption("For live-ish OHLC data, add a Twelve Data API key in Streamlit Secrets. Otherwise Yahoo Finance fallback is used.")
    run_analysis = st.button("Analyze latest candles", type="primary", use_container_width=True)

left, right = st.columns([1.5, 1])
with left:
    st.subheader(f"TradingView chart — {symbol}")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_interval = {"15 min":"15", "1 hour":"60", "4 hours":"240", "1 day":"D"}[timeframe]
    html = f"""
    <div class="tradingview-widget-container" style="height:560px;width:100%">
      <div id="tradingview_chart" style="height:540px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget({{
        "autosize": true,
        "symbol": "{tv_symbol}",
        "interval": "{tv_interval}",
        "timezone": "Etc/UTC",
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
    components.html(html, height=570, scrolling=False)

con = connect_db()
df, source = get_data(symbol, timeframe)
with right:
    st.subheader("Price-action result")
    st.caption(f"OHLC source: {source}")
    if df.empty:
        st.error("Market data load nahi hua. Twelve Data API key check karein ya baad mein retry karein.")
        result = {"bias":"WAIT","pattern":"No data","details":"Could not fetch OHLC candles."}
    else:
        result = analyze_price_action(df)
        st.metric("Current candle close", f"{float(df.close.iloc[-1]):.5f}")
        st.metric("Bias", result["bias"])
        st.write("**Detected pattern(s)**")
        st.info(result["pattern"])
        st.write(result["details"])
        st.write("**Recent OHLC candles**")
        view = df.tail(10).copy()
        view.index = view.index.astype(str)
        st.dataframe(view, use_container_width=True)
        if run_analysis:
            log_analysis(con, symbol, timeframe, df, result)
            st.success("Analysis saved to the local price-action database.")
    if not run_analysis:
        st.caption("Press “Analyze latest candles” to save this result to the database.")

st.divider()
st.subheader("Saved price-action database")
logs = pd.read_sql_query(
    "SELECT created_at, symbol, timeframe, candle_time, close, pattern, bias, details FROM price_action_log ORDER BY id DESC LIMIT 100",
    con,
)
if logs.empty:
    st.write("Abhi database mein saved analysis nahi hai.")
else:
    st.dataframe(logs, use_container_width=True)
    csv = logs.to_csv(index=False).encode("utf-8")
    st.download_button("Download analysis log CSV", csv, "bigman_price_action_log.csv", "text/csv")
con.close()

st.warning(
    "Important: TradingView yahan chart ke liye embedded hai. TradingView se direct OHLC database/API feed is code mein nahi hai; "
    "analysis Twelve Data (agar API key set hai) ya Yahoo Finance fallback se hota hai. Streamlit Cloud ki local SQLite file restart/redeploy par "
    "persist hone ki guarantee nahi hai. Persistent database ke liye hosted PostgreSQL/other durable DB configure karein. No broker orders are sent."
)
