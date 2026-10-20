import json
import sqlite3
import time
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import yfinance as yf

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
try:
    DATA_DIR.mkdir(exist_ok=True)
    _probe = DATA_DIR / ".write_test"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink(missing_ok=True)
    DB_PATH = DATA_DIR / "big_man.db"
except OSError:
    DB_PATH = Path("/tmp") / "big_man.db"
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


def utc_now_str():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    # SQLite connection with timeout and retry handling for concurrency/locks + WAL mode where supported
    for attempt in range(5):
        try:
            con = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA journal_mode=WAL;")
            except Exception:
                pass
            return con
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.5)


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
            exit_time TEXT,
            duration TEXT,
            pnl REAL,
            exit_reason TEXT,
            strategy_note TEXT,
            balance_updated INTEGER DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        defaults = {
            "virtual_balance": "10000",
            "auto_paper": "0",
            "last_processed_candles": "{}",
            "last_trade_candles": "{}",
        }
        for key, value in defaults.items():
            existing = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if not existing:
                con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, value))
                
        # Safe migrations for paper_trades table including balance_updated flag
        columns = {row[1] for row in con.execute("PRAGMA table_info(paper_trades)").fetchall()}
        migrations = {
            "timeframe": "TEXT NOT NULL DEFAULT 'H1'",
            "exit_time": "TEXT",
            "duration": "TEXT",
            "exit_reason": "TEXT",
            "strategy_note": "TEXT",
            "balance_updated": "INTEGER DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {definition}")
                # Requirement 7: Prevent old closed trades from adding P/L again during migration
                if name == "balance_updated":
                    con.execute("UPDATE paper_trades SET balance_updated = 1 WHERE status = 'CLOSED'")


def get_setting(key, default=""):
    try:
        with db_connect() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_setting(key, value):
    try:
        with db_connect() as con:
            existing = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if existing:
                con.execute("UPDATE app_settings SET value=? WHERE key=?", (str(value), key))
            else:
                con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, str(value)))
    except Exception as exc:
        st.warning(f"Setting save nahi ho saki: {exc}")


def con_set_setting(con, key, value):
    existing = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if existing:
        con.execute("UPDATE app_settings SET value=? WHERE key=?", (str(value), key))
    else:
        con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, str(value)))


def load_processed_candles():
    try:
        result = json.loads(get_setting("last_processed_candles", "{}"))
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_candles(ticker, interval, period):
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
            return pd.DataFrame(), f"Yahoo Finance ne {ticker} ke liye data nahi diya. Fresh market data abhi unavailable ho sakta hai."
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).strip() for c in raw.columns]
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
        return pd.DataFrame(), f"Market data fetch error (Fresh market data unavailable): {type(exc).__name__}: {exc}"


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


def evaluate_and_close_trades(con, symbol, last_candle):
    """Evaluate open paper trades against SL/TP or 15-minute maximum duration limit atomically with virtual balance updates."""
    trades = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' AND symbol=?", (symbol,)).fetchall()
    closed = 0
    hi, lo = float(last_candle["high"]), float(last_candle["low"])
    current_close = float(last_candle["close"])
    now_utc = datetime.now(timezone.utc)
    
    for trade in trades:
        try:
            created_time = pd.Timestamp(trade["created_at"])
            if created_time.tzinfo is None:
                created_time = created_time.tz_localize("UTC")
        except (TypeError, ValueError):
            continue

        side = trade["side"]
        stop, target, entry, qty = float(trade["stop"]), float(trade["target"]), float(trade["entry"]), float(trade["quantity"])
        exit_price, reason = None, None

        # 1. Check Stop Loss / Take Profit first
        if side == "BUY":
            if lo <= stop:
                exit_price, reason = stop, "Stop Loss"
            elif hi >= target:
                exit_price, reason = target, "Take Profit"
        else:
            if hi >= stop:
                exit_price, reason = stop, "Stop Loss"
            elif lo <= target:
                exit_price, reason = target, "Take Profit"

        # 2. Check 15-minute maximum duration time exit
        elapsed_minutes = (now_utc - created_time.to_pydatetime()).total_seconds() / 60.0
        if exit_price is None and elapsed_minutes >= 15.0:
            exit_price, reason = current_close, "Time Exit"

        if exit_price is not None:
            exit_time_str = utc_now_str()
            duration_delta = now_utc - created_time.to_pydatetime()
            mins = int(duration_delta.total_seconds() // 60)
            secs = int(duration_delta.total_seconds() % 60)
            duration_str = f"{mins}m {secs}s"

            if side == "BUY":
                pnl = (exit_price - entry) * qty
            else:
                pnl = (entry - exit_price) * qty

            # Requirements 1, 2, 3, 4, 5: Update virtual balance atomically and exactly once
            balance_updated = int(trade["balance_updated"] if "balance_updated" in trade.keys() else 0)
            if balance_updated == 0:
                bal_row = con.execute("SELECT value FROM app_settings WHERE key='virtual_balance'").fetchone()
                current_balance = float(bal_row["value"]) if bal_row else 10000.0
                new_balance = current_balance + pnl
                con.execute("UPDATE app_settings SET value=? WHERE key='virtual_balance'", (str(new_balance),))
                balance_updated = 1

            con.execute(
                """UPDATE paper_trades 
                   SET status='CLOSED', exit_price=?, exit_time=?, duration=?, pnl=?, exit_reason=?, balance_updated=? 
                   WHERE id=?""",
                (exit_price, exit_time_str, duration_str, pnl, reason, balance_updated, trade["id"])
            )
            closed += 1
    return closed


def create_auto_trade(con, symbol, timeframe, side, last_candle, atr, balance, signals):
    # Rule: Maximum of 3 simultaneously open paper trades
    open_count = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
    if open_count >= 3:
        return False, "Maximum 3 open paper trades ki limit poori ho chuki hai."
    
    entry = float(last_candle["close"])
    stop_distance = max(float(atr) * 1.5, entry * 0.001)
    stop = entry - stop_distance if side == "BUY" else entry + stop_distance
    target = entry + stop_distance * 1.5 if side == "BUY" else entry - stop_distance * 1.5
    risk_cash = max(1.0, float(balance) * 0.005)
    qty = min(risk_cash / stop_distance, 10000.0)
    note = ", ".join(signals.loc[signals["Signal"] == side, "Strategy"].tolist())
    
    con.execute("""INSERT INTO paper_trades(created_at,symbol,timeframe,side,entry,stop,target,quantity,status,strategy_note,balance_updated)
                  VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?, 0)""", (utc_now_str(), symbol, timeframe, side, entry, stop, target, qty, note))
    return True, f"Demo paper {side} trade created."


# Background monitoring thread running at least once per minute where infrastructure permits
def background_monitor_worker():
    while True:
        time.sleep(60)
        try:
            with db_connect() as con:
                open_trades = con.execute("SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'").fetchall()
                for ot in open_trades:
                    sym = ot["symbol"]
                    raw = yf.download(sym, interval="15m", period="1d", progress=False, threads=False)
                    if not raw.empty:
                        if isinstance(raw.columns, pd.MultiIndex):
                            raw.columns = raw.columns.get_level_values(0)
                        raw.columns = [str(c).strip() for c in raw.columns]
                        if {"Open", "High", "Low", "Close"}.issubset(raw.columns):
                            data = raw.reset_index()
                            time_col = "Datetime" if "Datetime" in data.columns else "Date"
                            if time_col not in data.columns:
                                time_col = data.columns[0]
                            data = data.rename(columns={time_col: "time", "Open": "open", "High": "high", "Low": "low", "Close": "close"})
                            data["time"] = pd.to_datetime(data["time"], errors="coerce", utc=True)
                            last_row = data.iloc[-1]
                            evaluate_and_close_trades(con, sym, last_row)
        except Exception:
            pass


@st.cache_resource
def start_background_monitor():
    t = threading.Thread(target=background_monitor_worker, daemon=True)
    t.start()
    return True


init_db()
start_background_monitor()

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
    new_auto = st.toggle("Auto paper trading", value=auto_enabled, help="Creates simulated trades when app runs/refreshes. Hosting limitation: Streamlit Cloud sleeps when inactive, so active browser tab or manual/scheduled refresh is required for continuous monitoring.")
    if new_auto != auto_enabled:
        set_setting("auto_paper", "1" if new_auto else "0")
        st.rerun()
    st.button("🔄 Refresh market data", on_click=st.cache_data.clear, use_container_width=True)
    st.caption("Hosting note: Streamlit Cloud par background daemon service continuously sleep/inactive ho sakti hai jab app active na ho. Background thread tab tak hi monitor karta hai jab tak app server process alive aur running state mein ho.")

balance = float(get_setting("virtual_balance", "10000") or 10000)
data, fetch_error = fetch_candles(ticker, interval, period)

analyzer_tab, library_tab, paper_tab, setup_tab = st.tabs(["📊 Analyzer", "🧠 Strategy Library", "🧾 Paper Trading", "⚙️ Setup & Limits"])

with analyzer_tab:
    st.subheader(f"{pair_label} · {timeframe_label}")
    
    # Check open trades across all symbols for time-based / SL-TP exits on page load/refresh
    with db_connect() as con:
        all_open_trades = con.execute("SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'").fetchall()
        for ot in all_open_trades:
            sym = ot["symbol"]
            sym_interval = timeframe_code if sym == ticker else "60m"
            sym_period = period if sym == ticker else "1mo"
            s_data, s_err = fetch_candles(sym, sym_interval, sym_period)
            if not s_data.empty:
                evaluate_and_close_trades(con, sym, s_data.iloc[-1])

    if fetch_error or data.empty:
        st.error(fetch_error or "Market data unavailable.")
        st.info("Internet connection check karein, phir Refresh market data dabayein. Fresh market data abhi unavailable ho sakta hai.")
    elif len(data) < 3:
        st.warning("Signal banane ke liye abhi candles kam hain. Baad mein refresh karein.")
    else:
        analyzed, votes, atr = strategy_signals(data)
        last = analyzed.iloc[-1]
        buy_votes = int((votes["Signal"] == "BUY").sum())
        sell_votes = int((votes["Signal"] == "SELL").sum())
        signal = "BUY" if buy_votes >= vote_threshold else "SELL" if sell_votes >= vote_threshold else "WAIT"
        candle_time = pd.Timestamp(last["time"]).isoformat()
        pair_time_key = f"{ticker}|{timeframe_code}"
        
        try:
            last_trade_candles = json.loads(get_setting("last_trade_candles", "{}"))
            if not isinstance(last_trade_candles, dict):
                last_trade_candles = {}
        except (TypeError, json.JSONDecodeError):
            last_trade_candles = {}
            
        with db_connect() as con:
            closed_count = evaluate_and_close_trades(con, ticker, last)
            if new_auto:
                if signal in ("BUY", "SELL"):
                    if last_trade_candles.get(pair_time_key) == candle_time:
                        st.info("Is candle par demo trade pehle hi create ho chuka hai; duplicate trade nahi banaya.")
                    else:
                        made, msg = create_auto_trade(con, ticker, timeframe_code, signal, last, atr, balance, votes)
                        if made:
                            last_trade_candles[pair_time_key] = candle_time
                            con_set_setting(con, "last_trade_candles", json.dumps(last_trade_candles))
                            st.success(msg)
                        else:
                            st.warning(f"Demo trade create nahi hua: {msg}")
                else:
                    st.caption("Signal WAIT hai, isliye naya paper trade nahi bana.")
            else:
                st.caption("Auto Paper Trading OFF hai. Demo trade banane ke liye sidebar mein ise ON karein.")
                
            if closed_count:
                st.success(f"{closed_count} paper trade(s) closed based on SL/TP or 15-minute Time Exit.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Combined signal", signal)
        c2.metric("BUY votes", f"{buy_votes}/8")
        c3.metric("SELL votes", f"{sell_votes}/8")
        c4.metric("Latest close", f"{float(last['close']):.5f}")
        st.caption(f"Mode: {signal_mode} · Signal threshold: {vote_threshold}/8 votes. Virtual balance accounting with atomic P/L integration enabled.")

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
    st.subheader("Paper Trading Journal & Balance Accounting")
    
    # Requirement 8: Show current virtual balance and total realized P/L clearly
    current_balance_val = float(get_setting("virtual_balance", "10000") or 10000)
    
    with db_connect() as journal_con:
        trades = pd.read_sql_query("""
            SELECT id AS 'Trade ID', 
                   symbol AS 'Currency Pair', 
                   side AS 'Side', 
                   entry AS 'Entry Price', 
                   created_at AS 'Entry Time', 
                   stop AS 'Stop Loss', 
                   target AS 'Take Profit', 
                   exit_price AS 'Exit Price', 
                   exit_time AS 'Exit Time', 
                   duration AS 'Duration', 
                   status AS 'Status', 
                   pnl AS 'P/L ($)', 
                   exit_reason AS 'Exit Reason' 
            FROM paper_trades 
            ORDER BY id DESC
        """, journal_con)

    if not trades.empty:
        closed_trades = trades[trades["Status"] == "CLOSED"]
        total_realized_pnl = pd.to_numeric(closed_trades["P/L ($)"], errors="coerce").fillna(0).sum()
    else:
        total_realized_pnl = 0.0

    bm1, bm2, bm3, bm4 = st.columns(4)
    bm1.metric("Virtual Balance", f"${current_balance_val:,.4f}")
    bm2.metric("Total Realized P/L", f"${total_realized_pnl:,.4f}")
    bm3.metric("Total Trades", len(trades))
    bm4.metric("Open Trades", int((trades["Status"] == "OPEN").sum()) if not trades.empty else 0)

    if trades.empty:
        st.info("Abhi koi paper trade nahi hai. Auto paper trading enable karne par qualifying signal par demo trade create ho sakta hai.")
    else:
        st.dataframe(trades, use_container_width=True, hide_index=True)
        st.download_button("Download trade journal CSV", trades.to_csv(index=False).encode("utf-8"), file_name="big_man_paper_trades.csv", mime="text/csv")
    st.caption("Accounting note: When a trade closes (Stop Loss, Take Profit, or Time Exit), its realized P/L is atomically added to the virtual balance exactly once via a secure transaction flag.")

with setup_tab:
    st.subheader("Setup & Important Limits")
    st.markdown("""
    - **Virtual Balance Accounting:** Realized P/L (`new_balance = current_balance + realized_pnl`) is updated atomically and exactly once per closed trade via database transaction flags (`balance_updated`).
    - **15-Minute Max Duration & Time Exit:** Maximum holding limit is 15 minutes. If SL/TP is not reached, trades exit at the latest market price as **Time Exit**.
    - **Simultaneous Trades Limit:** Maximum **3 open paper trades** simultaneously.
    - **Hosting & Monitoring Limitation:** Streamlit Cloud sleeps during inactivity. Active browser session or manual refresh is required for continuous monitoring.
    - **Paper Trading Only:** No real broker connection or real money is involved.
    """)
    if st.button("Clear cached market data"):
        st.cache_data.clear()
        st.success("Cache cleared. Page refresh karein.")
