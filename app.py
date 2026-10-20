import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import yfinance as yf

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "big_man.db"
STRATEGIES_PATH = APP_DIR / "strategies.json"

st.set_page_config(page_title="BIG MAN Forex AI", page_icon="📈", layout="wide")

PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
}
TIMEFRAMES = {
    "H1 (1 hour)": ("60m", "1mo"),
    "M15 (15 minutes)": ("15m", "5d"),
    "D1 (1 day)": ("1d", "6mo"),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            exit_price REAL,
            pnl REAL,
            exit_reason TEXT,
            strategy_note TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        defaults = {
            "virtual_balance": "10000",
            "auto_paper": "0",
            # Store last processed candle per symbol+timeframe, not globally.
            "last_processed_candles": "{}",
        }
        for key, value in defaults.items():
            con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", (key, value))
        # Safe migration for an older database.
        columns = {row[1] for row in con.execute("PRAGMA table_info(paper_trades)").fetchall()}
        migrations = {
            "timeframe": "TEXT NOT NULL DEFAULT 'H1'",
            "exit_reason": "TEXT",
            "strategy_note": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {definition}")
        # Upgrade the older single-candle setting without losing it.
        old = con.execute("SELECT value FROM app_settings WHERE key='last_processed_candle'").fetchone()
        new = con.execute("SELECT value FROM app_settings WHERE key='last_processed_candles'").fetchone()
        if old and new and new[0] == "{}" and old[0]:
            con.execute("UPDATE app_settings SET value=? WHERE key='last_processed_candles'", (json.dumps({"legacy": old[0]}),))


def get_setting(key, default=""):
    with db_connect() as con:
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with db_connect() as con:
        con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def load_processed_candles():
    try:
        result = json.loads(get_setting("last_processed_candles", "{}"))
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles(ticker, interval, period):
    """Fetch recent market data. Errors are returned to the UI, not silently hidden."""
    try:
        raw = yf.download(
            ticker,
            interval=interval,
            period=period,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty:
            return pd.DataFrame(), f"Yahoo Finance ne {ticker} ke liye data nahi diya. Thodi der baad refresh karein."
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.rename(columns={str(c).lower(): str(c).lower() for c in raw.columns})
        required = ["Open", "High", "Low", "Close"]
        missing = [col for col in required if col not in raw.columns]
        if missing:
            return pd.DataFrame(), f"Market data mein required columns missing hain: {', '.join(missing)}"
        data = raw.reset_index()
        time_col = "Datetime" if "Datetime" in data.columns else "Date"
        if time_col not in data.columns:
            time_col = data.columns[0]
        data = data.rename(columns={time_col: "time", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        data["time"] = pd.to_datetime(data["time"], errors="coerce", utc=True)
        for col in ["open", "high", "low", "close"]:
            data[col] = pd.to_numeric(data[col], errors="coerce")
        data = data.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").tail(500).reset_index(drop=True)
        if data.empty:
            return pd.DataFrame(), "Market data mila, lekin usable candles nahi mile."
        return data, None
    except Exception as exc:
        return pd.DataFrame(), f"Market data fetch error: {type(exc).__name__}: {exc}"


def strategy_signals(data):
    df = data.copy()
    df["sma20"] = df["close"].rolling(20, min_periods=5).mean()
    df["sma50"] = df["close"].rolling(50, min_periods=10).mean()
    df["prior_high"] = df["high"].rolling(20, min_periods=5).max().shift(1)
    df["prior_low"] = df["low"].rolling(20, min_periods=5).min().shift(1)
    df["range"] = (df["high"] - df["low"]).abs()
    df["atr14"] = df["range"].rolling(14, min_periods=5).mean()
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    close = float(last["close"])
    atr = float(last["atr14"]) if pd.notna(last["atr14"]) else close * 0.001
    atr = max(atr, close * 0.0001)
    sma20 = float(last["sma20"]) if pd.notna(last["sma20"]) else close
    sma50 = float(last["sma50"]) if pd.notna(last["sma50"]) else close
    prior_high = float(last["prior_high"]) if pd.notna(last["prior_high"]) else float(df["high"].iloc[:-1].max())
    prior_low = float(last["prior_low"]) if pd.notna(last["prior_low"]) else float(df["low"].iloc[:-1].min())

    signals = []
    def add(name, vote, note):
        signals.append({"Strategy": name, "Signal": vote, "Reason": note})

    add("Support / Resistance", "BUY" if close > prior_low + (prior_high-prior_low)*0.65 else "SELL" if close < prior_low + (prior_high-prior_low)*0.35 else "WAIT", "Price position inside recent 20-candle range")
    fib_low = prior_low + (prior_high-prior_low)*0.382
    fib_high = prior_low + (prior_high-prior_low)*0.618
    add("Fibonacci Zone (proxy)", "BUY" if close > fib_high else "SELL" if close < fib_low else "WAIT", "Simple 38.2%–61.8% range proxy; not a full Fibonacci setup")
    add("Breakout", "BUY" if close > prior_high else "SELL" if close < prior_low else "WAIT", "Close compared with previous range; levels exclude current candle")
    broke_up = float(prev["close"]) > prior_high if pd.notna(last["prior_high"]) else False
    broke_dn = float(prev["close"]) < prior_low if pd.notna(last["prior_low"]) else False
    add("Fake Breakout (proxy)", "SELL" if broke_up and close < prior_high else "BUY" if broke_dn and close > prior_low else "WAIT", "Simple false-break proxy; review manually")
    add("Trend Alignment", "BUY" if sma20 > sma50 and close > sma20 else "SELL" if sma20 < sma50 and close < sma20 else "WAIT", "SMA20/SMA50 trend filter")
    add("Two-Candle Price Action", "BUY" if close > float(prev["close"]) and close > float(last["open"]) else "SELL" if close < float(prev["close"]) and close < float(last["open"]) else "WAIT", "Basic two-candle momentum proxy")
    add("SMA Touch / Bounce (proxy)", "BUY" if close >= sma20 and float(last["low"]) <= sma20 and sma20 >= sma50 else "SELL" if close <= sma20 and float(last["high"]) >= sma20 and sma20 <= sma50 else "WAIT", "SMA20 touch with trend filter")
    midpoint = (prior_high + prior_low) / 2
    add("Range Supply / Demand (proxy)", "BUY" if close < prior_low + (prior_high-prior_low)*0.25 else "SELL" if close > prior_low + (prior_high-prior_low)*0.75 else "WAIT", f"Range location; midpoint {midpoint:.5f}")
    return df, pd.DataFrame(signals), atr


def close_open_trades_on_candle(con, symbol, last_candle):
    """Evaluate every open paper trade against the currently available candle for its symbol only."""
    trades = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' AND symbol=?", (symbol,)).fetchall()
    closed = 0
    hi, lo = float(last_candle["high"]), float(last_candle["low"])
    for trade in trades:
        side = trade["side"]
        stop, target, entry, qty = float(trade["stop"]), float(trade["target"]), float(trade["entry"]), float(trade["quantity"])
        exit_price, reason = None, None
        # Conservative assumption: if both are touched within one candle, stop-loss is hit first.
        if side == "BUY":
            if lo <= stop:
                exit_price, reason = stop, "Stop Loss"
            elif hi >= target:
                exit_price, reason = target, "Take Profit"
            pnl = (exit_price-entry)*qty if exit_price is not None else None
        else:
            if hi >= stop:
                exit_price, reason = stop, "Stop Loss"
            elif lo <= target:
                exit_price, reason = target, "Take Profit"
            pnl = (entry-exit_price)*qty if exit_price is not None else None
        if exit_price is not None:
            con.execute("UPDATE paper_trades SET status='CLOSED', exit_price=?, pnl=?, exit_reason=? WHERE id=?", (exit_price, pnl, reason, trade["id"]))
            closed += 1
    return closed


def create_auto_trade(con, symbol, timeframe, side, last_candle, atr, balance, signals):
    # Keep one open trade total, to make the demo risk easier to understand.
    open_count = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
    if open_count:
        return False, "Pehle se ek open paper trade hai; naya trade nahi banaya."
    entry = float(last_candle["close"])
    stop_distance = max(float(atr) * 1.5, entry * 0.001)
    stop = entry - stop_distance if side == "BUY" else entry + stop_distance
    target = entry + stop_distance * 1.5 if side == "BUY" else entry - stop_distance * 1.5
    risk_cash = max(1.0, float(balance) * 0.005)
    qty = min(risk_cash / stop_distance, 10000.0)
    note = ", ".join(signals.loc[signals["Signal"] == side, "Strategy"].tolist())
    con.execute("""INSERT INTO paper_trades(created_at,symbol,timeframe,side,entry,stop,target,quantity,status,strategy_note)
                  VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?)""", (utc_now(), symbol, timeframe, side, entry, stop, target, qty, note))
    return True, f"Demo paper {side} trade created."


init_db()

st.title("📈 BIG MAN — Forex AI (Demo)")
st.caption("Educational prototype • Yahoo Finance data • Paper trading only • No broker connection")

with st.sidebar:
    st.header("Settings")
    pair_label = st.selectbox("Currency pair", list(PAIRS.keys()))
    timeframe_label = st.selectbox("Timeframe", list(TIMEFRAMES.keys()))
    signal_mode = st.selectbox(
        "Signal mode",
        ["Safe Mode", "Fast Mode"],
        index=0,
        help="Safe Mode: 5/8 votes. Fast Mode: 3/8 votes, signals zyada aa sakte hain lekin false signals bhi badhenge.",
    )
    vote_threshold = 5 if signal_mode == "Safe Mode" else 3
    ticker = PAIRS[pair_label]
    interval, period = TIMEFRAMES[timeframe_label]
    timeframe_code = interval
    balance_value = float(get_setting("virtual_balance", "10000") or 10000)
    new_balance = st.number_input("Virtual balance ($)", min_value=100.0, max_value=1_000_000.0, value=float(balance_value), step=100.0)
    if st.button("Save virtual balance", use_container_width=True):
        set_setting("virtual_balance", new_balance)
        st.success("Virtual balance saved.")
    auto_enabled = get_setting("auto_paper", "0") == "1"
    new_auto = st.toggle("Auto paper trading", value=auto_enabled, help="Creates simulated trades when you open/refresh the app. It does not run continuously in the background.")
    if new_auto != auto_enabled:
        set_setting("auto_paper", "1" if new_auto else "0")
        st.rerun()
    st.button("🔄 Refresh market data", on_click=st.cache_data.clear, use_container_width=True)
    st.caption("Forex market data may be delayed or temporarily unavailable.")

balance = float(get_setting("virtual_balance", "10000") or 10000)
con = db_connect()
data, fetch_error = fetch_candles(ticker, interval, period)

analyzer_tab, library_tab, paper_tab, setup_tab = st.tabs(["📊 Analyzer", "🧠 Strategy Library", "🧾 Paper Trading", "⚙️ Setup & Limits"])

with analyzer_tab:
    st.subheader(f"{pair_label} · {timeframe_label}")
    if fetch_error or data.empty:
        st.error(fetch_error or "Market data unavailable.")
        st.info("Internet connection check karein, phir Refresh market data dabayein. Yahoo Finance kabhi-kabhi intraday data limit kar sakta hai.")
    elif len(data) < 3:
        st.warning("Signal banane ke liye abhi candles kam hain. Baad mein refresh karein.")
    else:
        analyzed, votes, atr = strategy_signals(data)
        last = analyzed.iloc[-1]
        buy_votes = int((votes["Signal"] == "BUY").sum())
        sell_votes = int((votes["Signal"] == "SELL").sum())
        signal = "BUY" if buy_votes >= vote_threshold else "SELL" if sell_votes >= vote_threshold else "WAIT"
        candle_time = pd.Timestamp(last["time"]).isoformat()
        candle_key = f"{ticker}|{timeframe_code}|{candle_time}"
        processed = load_processed_candles()
        already_processed = processed.get(f"{ticker}|{timeframe_code}") == candle_time
        closed_count = close_open_trades_on_candle(con, ticker, last)
        if new_auto and not already_processed:
            if signal in ("BUY", "SELL"):
                made, msg = create_auto_trade(con, ticker, timeframe_code, signal, last, atr, balance, votes)
                st.info(msg)
            else:
                st.caption("Signal WAIT hai, isliye naya paper trade nahi bana.")
            processed[f"{ticker}|{timeframe_code}"] = candle_time
            set_setting("last_processed_candles", json.dumps(processed))
        if closed_count:
            st.success(f"{closed_count} paper trade(s) closed based on the latest available candle.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Combined signal", signal)
        c2.metric("BUY votes", f"{buy_votes}/8")
        c3.metric("SELL votes", f"{sell_votes}/8")
        c4.metric("Latest close", f"{float(last['close']):.5f}")
        st.caption(f"Mode: {signal_mode} · Signal threshold: {vote_threshold}/8 votes. Fast Mode mein false signals zyada ho sakte hain. Signal educational proxy hai, guaranteed prediction nahi. Latest Yahoo candle abhi forming ho sakti hai; ise confirmed closed-candle signal na samjhein.")

        fig = go.Figure(data=[go.Candlestick(x=analyzed["time"], open=analyzed["open"], high=analyzed["high"], low=analyzed["low"], close=analyzed["close"], name="Candles")])
        fig.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma20"], name="SMA 20", mode="lines"))
        fig.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma50"], name="SMA 50", mode="lines"))
        fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.subheader("8 strategy votes")
        st.dataframe(votes, use_container_width=True, hide_index=True)

with library_tab:
    st.subheader("Strategy Library")
    if STRATEGIES_PATH.exists():
        try:
            content = json.loads(STRATEGIES_PATH.read_text(encoding="utf-8"))
            st.json(content)
        except (json.JSONDecodeError, OSError) as exc:
            st.warning(f"strategies.json read nahi ho saka: {exc}")
    else:
        st.info("strategies.json file nahi mili. Neeche app ke 8 built-in educational proxy strategies hain.")
        st.dataframe(pd.DataFrame([
            {"Strategy": "Support / Resistance", "Purpose": "Recent range mein price location"},
            {"Strategy": "Fibonacci Zone (proxy)", "Purpose": "38.2%–61.8% range zone"},
            {"Strategy": "Breakout", "Purpose": "Previous range high/low break"},
            {"Strategy": "Fake Breakout (proxy)", "Purpose": "Possible failed breakout"},
            {"Strategy": "Trend Alignment", "Purpose": "SMA20/SMA50 trend filter"},
            {"Strategy": "Two-Candle Price Action", "Purpose": "Basic candle momentum"},
            {"Strategy": "SMA Touch / Bounce (proxy)", "Purpose": "SMA20 touch with trend filter"},
            {"Strategy": "Range Supply / Demand (proxy)", "Purpose": "Recent range extremes"},
        ]), use_container_width=True, hide_index=True)
    st.warning("Ye strategies simplified proxies hain; proper backtesting, spread/slippage aur out-of-sample validation nahi hua hai.")

with paper_tab:
    st.subheader("Paper Trading Journal")
    with db_connect() as journal_con:
        trades = pd.read_sql_query("SELECT id,created_at,symbol,timeframe,side,entry,stop,target,quantity,status,exit_price,pnl,exit_reason,strategy_note FROM paper_trades ORDER BY id DESC", journal_con)
    if trades.empty:
        st.info("Abhi koi paper trade nahi hai. Auto paper trading enable karne par qualifying signal par demo trade create ho sakta hai.")
    else:
        closed_trades = trades[trades["status"] == "CLOSED"]
        total_pnl = pd.to_numeric(closed_trades["pnl"], errors="coerce").fillna(0).sum()
        m1, m2, m3 = st.columns(3)
        m1.metric("Total paper trades", len(trades))
        m2.metric("Open trades", int((trades["status"] == "OPEN").sum()))
        m3.metric("Closed P/L ($)", f"{total_pnl:.2f}")
        st.dataframe(trades, use_container_width=True, hide_index=True)
        st.download_button("Download trade journal CSV", trades.to_csv(index=False).encode("utf-8"), file_name="big_man_paper_trades.csv", mime="text/csv")
    st.caption("Trade exits sirf us symbol ko analyze/refresh karne par check hote hain. App continuously background mein nahi chalta.")

with setup_tab:
    st.subheader("Setup & Important Limits")
    st.markdown("""
    - **Market data:** Yahoo Finance (`yfinance`); availability, delay aur limits badal sakte hain.
    - **Strategies:** 8 simplified rule-based proxies; inhe validated AI model ya profitable system na samjhein.
    - **Signal modes:** Safe Mode mein 5/8 votes chahiye; Fast Mode mein 3/8 votes. Fast Mode zyada signals de sakta hai, par false signals ka risk bhi zyada hai.
    - **Auto paper mode:** App run/refresh hone par hi evaluation hoti hai; 24/7 background service nahi.
    - **Risk model:** Demo trade mein nominal stop distance aur virtual balance ka 0.5% risk budget use hota hai. Forex contract sizing, pip value, spread, commission aur slippage ka exact broker model nahi hai.
    - **Storage:** SQLite file deployment environment par depend karti hai; Streamlit redeploy/restart ke baad data persist rehne ki guarantee nahi.
    - **Trading:** Koi broker connected nahi hai. Ye app real orders place nahi karti. Real money use karne se pehle independent testing aur financial advice zaroori hai.
    """)
    if st.button("Clear cached market data"):
        st.cache_data.clear()
        st.success("Cache cleared. Page refresh karein.")

con.close()
