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

st.set_page_config(page_title="BIG MAN2 | Price Action & Trade Journal", page_icon="📈", layout="wide")

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
        CREATE TABLE IF NOT EXISTS trade_journal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            action TEXT NOT NULL,
            pattern TEXT NOT NULL,
            bias TEXT NOT NULL,
            notes TEXT
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

def analyze_8_strategies(df):
    if len(df) < 30:
        return {"bias":"WAIT", "pattern":"Kam candles hain", "details":"Kam se kam 30 candles chahiye."}
    
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
    
    strategies = []
    bullish_score = 0
    bearish_score = 0

    # 1. SMC FVG
    if last.low > p_prev.high and prev.close > prev.open:
        strategies.append("Bullish FVG")
        bullish_score += 2
    elif last.high < p_prev.low and prev.close < prev.open:
        strategies.append("Bearish FVG")
        bearish_score += 2

    # 2. Premium / Discount
    if last.close < eq_level:
        strategies.append("Discount Zone")
        bullish_score += 1
    else:
        strategies.append("Premium Zone")
        bearish_score += 1

    # 3. Engulfing
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        strategies.append("Bullish Engulfing")
        bullish_score += 2
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        strategies.append("Bearish Engulfing")
        bearish_score += 2

    # 4. Pin Bar
    if lower_wick >= body * 2 and lower_wick >= rng * 0.45 and last.close > last.open:
        strategies.append("Bullish Pin Bar")
        bullish_score += 2
    elif upper_wick >= body * 2 and upper_wick >= rng * 0.45 and last.close < last.open:
        strategies.append("Bearish Pin Bar")
        bearish_score += 2

    # 5. Breakout
    if last.close > prior_high:
        strategies.append("High Breakout")
        bullish_score += 2
    elif last.close < prior_low:
        strategies.append("Low Breakout")
        bearish_score += 2

    # 6. Momentum
    if body > rng * 0.7:
        if last.close > last.open:
            strategies.append("Strong Momentum Up")
            bullish_score += 1
        else:
            strategies.append("Strong Momentum Down")
            bearish_score += 1

    # 7. Trend (SMA)
    sma20 = float(df.close.tail(20).mean())
    sma50 = float(df.close.tail(50).mean()) if len(df) >= 50 else sma20
    if sma20 > sma50:
        strategies.append("Uptrend (SMA)")
        bullish_score += 1
    else:
        strategies.append("Downtrend (SMA)")
        bearish_score += 1

    # 8. Inside Bar
    if last.high < prev.high and last.low > prev.low:
        strategies.append("Inside Bar")

    if bullish_score > bearish_score and bullish_score >= 3:
        bias = "BUY"
    elif bearish_score > bullish_score and bearish_score >= 3:
        bias = "SELL"
    else:
        bias = "WAIT"

    if not strategies:
        strategies = ["No Clear Setup"]

    # Calculate SL and TP based on ATR / recent swing
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

    return {
        "bias": bias,
        "pattern": ", ".join(strategies),
        "entry": current_close,
        "sl": sl,
        "tp": tp,
        "details": f"Zone: {zone} | Bullish: {bullish_score} | Bearish: {bearish_score}"
    }

def log_trade(con, symbol, timeframe, result, action_type, notes):
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    con.execute(
        "INSERT INTO trade_journal_log(created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, action, pattern, bias, notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, result["entry"], result["sl"], result["tp"], action_type, result["pattern"], result["bias"], notes)
    )
    con.commit()

# --- UI DESIGN ---
st.title("📈 BIG MAN2 — Price Action & Trade Journal")
st.caption("8 Strategies Confluence + Paper Trading Journal + SL/TP Management (IST Timezone)")

with st.sidebar:
    st.header("⚙️ Market Settings")
    symbol = st.selectbox("Pair Select Karein", list(SYMBOLS.keys()))
    timeframe = st.selectbox("Timeframe Select Karein", list(INTERVALS.keys()), index=1)
    
    st.markdown("---")
    st.subheader("📓 Paper Trade Journal Entry")
    custom_notes = st.text_input("Trade Notes / Setup Name", value="SMC FVG Confluence Setup")
    col_j1, col_j2 = st.columns(2)
    save_buy = col_j1.button("🟢 Log BUY Trade", use_container_width=True)
    save_sell = col_j2.button("🔴 Log SELL Trade", use_container_width=True)

con = connect_db()
df, source = get_data(symbol, timeframe)

if not df.empty:
    result = analyze_8_strategies(df)
    
    if save_buy:
        log_trade(con, symbol, timeframe, result, "PAPER BUY", custom_notes)
        st.sidebar.success("Paper BUY Trade Saved to Journal!")
    elif save_sell:
        log_trade(con, symbol, timeframe, result, "PAPER SELL", custom_notes)
        st.sidebar.error("Paper SELL Trade Saved to Journal!")

    col1, col2, col3 = st.columns(3)
    col1.metric("Live Market Rate", f"{result['entry']:.5f}")
    
    if result["bias"] == "BUY":
        col2.success("🟢 BIAS: BUY SETUP")
    elif result["bias"] == "SELL":
        col2.error("🔴 BIAS: SELL SETUP")
    else:
        col2.warning("🟡 BIAS: WAIT / CONSOLIDATION")
        
    col3.info(f"**Calculated SL & TP:**\nSL: {result['sl']:.5f} | TP: {result['tp']:.5f}")

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
    st.subheader("💡 8-Strategy Analysis & Levels")
    if df.empty:
        st.error("Market data load nahi ho pa raha hai.")
    else:
        st.write(f"**Strategies Triggered:** {result['pattern']}")
        st.write(result["details"])
        st.markdown("---")
        st.markdown(f"* **Suggested Entry:** `{result['entry']:.5f}`")
        st.markdown(f"* **Suggested Stop Loss (SL):** `{result['sl']:.5f}`")
        st.markdown(f"* **Suggested Take Profit (TP):** `{result['tp']:.5f}`")
        
        st.markdown("---")
        st.write("**Recent Candles (IST):**")
        view = df.tail(5).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()

st.subheader("📋 Saved Paper Trade Journal (IST Time)")
try:
    logs = pd.read_sql_query(
        "SELECT created_at_ist, symbol, timeframe, action, entry_price, stop_loss, take_profit, bias, pattern, notes FROM trade_journal_log ORDER BY id DESC LIMIT 50",
        con,
    )
    if logs.empty:
        st.info("Abhi journal me koi trade save nahi hai. Sidebar se 'Log BUY/SELL Trade' dabayein.")
    else:
        logs.columns = ['Time', 'Pair', 'TF', 'Action', 'Entry', 'SL', 'TP', 'Bias', 'Strategies', 'Notes']
        st.dataframe(logs, use_container_width=True)
except Exception:
    st.info("Journal database table update ho rahi hai.")

con.close()
