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

st.set_page_config(page_title="BIG MAN2 | Master Trading Bot", page_icon="📈", layout="wide")

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

def evaluate_master_strategies(df):
    if len(df) < 30:
        return {"bias":"WAIT", "votes":"Kam data hai", "bullish":0, "bearish":0, "entry":0, "sl":0, "tp":0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    # Swing High & Low for Fibonacci Equilibrium & Supply/Demand
    swing_high = float(df.high.tail(50).max())
    swing_low = float(df.low.tail(50).min())
    diff = swing_high - swing_low
    
    # Fibonacci Levels (0.5 Equilibrium and 0.618 OTE)
    eq_50 = swing_high - (diff * 0.50)
    ote_618 = swing_high - (diff * 0.618)
    
    bullish_votes = []
    bearish_votes = []

    # 1. Fair Value Gap (FVG) Strategy
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG Imbalance")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG Imbalance")

    # 2. Premium / Discount Zone (Fibonacci 50% Equilibrium)
    if last.close < eq_50:
        bullish_votes.append("Discount Zone (Below 0.5 EQ)")
    else:
        bearish_votes.append("Premium Zone (Above 0.5 EQ)")

    # 3. Fibonacci OTE (0.618 Confluence)
    if abs(last.close - ote_618) <= (diff * 0.05):
        if last.close > ote_618:
            bullish_votes.append("Fibonacci 0.618 OTE Buy Bounce")
        else:
            bearish_votes.append("Fibonacci 0.618 OTE Sell Rejection")

    # 4. Supply & Demand Zone Rejection
    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Zone Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Zone Wick Rejection")

    # 5. Engulfing Pattern
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.close <= prev.open:
        bearish_votes.append("Bearish Engulfing")

    # 6. Breakout Strategy
    recent = df.iloc[-21:-1]
    prior_high = float(recent.high.max())
    prior_low = float(recent.low.min())
    if last.close > prior_high:
        bullish_votes.append("Resistance Breakout")
    elif last.close < prior_low:
        bearish_votes.append("Support Breakdown")

    # 7. Momentum Candle
    if body > rng * 0.6:
        if last.close > last.open:
            bullish_votes.append("Strong Bullish Momentum")
        else:
            bearish_votes.append("Strong Bearish Momentum")

    # 8. Moving Average Trend (SMA 20 vs 50)
    sma20 = float(df.close.tail(20).mean())
    sma50 = float(df.close.tail(50).mean()) if len(df) >= 50 else sma20
    if sma20 > sma50:
        bullish_votes.append("Uptrend SMA Context")
    else:
        bullish_votes.append("Downtrend SMA Context")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    # Flexible Threshold: Agar 2 ya usse zyada votes mil jayein toh trade bias banega
    if b_score > s_score and b_score >= 2:
        bias = "BUY"
    elif s_score > b_score and s_score >= 2:
        bias = "SELL"
    else:
        # Default fallback to active momentum if scores are tied
        bias = "BUY" if last.close > prev.close else "SELL"

    current_close = float(last.close)
    if bias == "BUY":
        sl = current_close - (rng * 1.5)
        tp = current_close + (rng * 3.0)
    else:
        sl = current_close + (rng * 1.5)
        tp = current_close - (rng * 3.0)

    all_votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes) if bullish_votes else 'None'} | Bearish [{s_score}]: {', '.join(bearish_votes) if bearish_votes else 'None'}"

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

# --- STREAMLIT UI ---
st.title("📈 BIG MAN2 — Master Video-Based Trading Bot")
st.caption("Supply & Demand, FVG, Fibonacci OTE (0.618), Equilibrium Zones & 8-Strategy Voting Engine.")

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Currency Pair", list(SYMBOLS.keys()))
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.toast("Market refreshed successfully!", icon="🔄")
    
    st.markdown("---")
    st.subheader("⚡ Auto-Trading Bot")
    auto_mode = st.toggle("Enable Auto-Pilot", value=False)
    if auto_mode:
        st.success("🟢 Bot is actively trading based on video strategies!")
    else:
        st.warning("🟡 Bot is in Monitoring mode.")
        
    st.markdown("---")
    if st.button("🔴 Close Open Trades", use_container_width=True):
        con_temp = connect_db()
        df_temp, _ = get_data(symbol, timeframe)
        if not df_temp.empty:
            close_open_trades(con_temp, symbol, float(df_temp.close.iloc[-1]))
            st.sidebar.success("Trades closed!")
        con_temp.close()

con = connect_db()
df, source = get_data(symbol, timeframe)

if not df.empty:
    result = evaluate_master_strategies(df)
    
    if auto_mode:
        execute_auto_trade(con, symbol, timeframe, result)

    st.markdown("### 📊 Expert Voting & Strategy Dashboard")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Live Price", f"{result['entry']:.5f}")
    col2.metric("Bullish Votes", f"{result['bullish']} / 8")
    col3.metric("Bearish Votes", f"{result['bearish']} / 8")
    
    if result["bias"] == "BUY":
        col4.success("🟢 MASTER SIGNAL: BUY")
    else:
        col4.error("🔴 MASTER SIGNAL: SELL")

    st.info(f"**Video Strategy Confluence:** {result['votes']}")
    
    c_l1, c_l2 = st.columns(2)
    c_l1.metric("Stop Loss (SL)", f"{result['sl']:.5f}")
    c_l2.metric("Take Profit (TP)", f"{result['tp']:.5f}")

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
    st.subheader("💡 Recent Candles (IST)")
    if df.empty:
        st.error("Data load nahi ho pa raha hai.")
    else:
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()

st.subheader("📋 Bot Trade Journal & History (IST Time)")
try:
    logs = pd.read_sql_query(
        "SELECT created_at_ist, symbol, timeframe, status, bias, entry_price, stop_loss, take_profit, exit_price, pnl, strategy_votes FROM auto_trade_journal ORDER BY id DESC LIMIT 50",
        con,
    )
    if logs.empty:
        st.info("Abhi koi trade execute nahi hua hai.")
    else:
        logs.columns = ['Time', 'Pair', 'TF', 'Status', 'Bias', 'Entry', 'SL', 'TP', 'Exit Price', 'PnL (Pips)', 'Votes Info']
        st.dataframe(logs, use_container_width=True)
except Exception:
    st.info("Journal update ho rahi hai.")

con.close()
