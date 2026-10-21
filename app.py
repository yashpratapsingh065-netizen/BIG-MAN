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

st.set_page_config(page_title="BIG MAN Forex AI Pro", page_icon="📈", layout="wide")

# ==========================================
# EXPERT UI: PREMIUM DARK MODE & BRANDING CSS
# ==========================================
st.markdown("""
<style>
    /* Global App Styling */
    .stApp {
        background-color: #0b0f19;
        color: #f3f4f6;
    }
    
    /* Branded Header */
    .brand-header {
        display: flex;
        align-items: center;
        gap: 15px;
        padding: 15px 0;
        border-bottom: 1px solid #1f2937;
        margin-bottom: 25px;
    }
    .brand-logo {
        font-size: 2.4rem;
        background: linear-gradient(135deg, #3b82f6 0%, #10b981 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        display: flex;
        align-items: center;
        justify-content: center;
        background-color: #111827;
        padding: 8px 14px;
        border-radius: 14px;
        border: 1px solid #374151;
        box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    }
    .brand-title {
        font-size: 1.9rem;
        font-weight: 800;
        color: #ffffff;
        letter-spacing: 0.5px;
        margin: 0;
    }
    .brand-subtitle {
        color: #9ca3af;
        font-size: 0.9rem;
        margin: 0;
    }

    /* Premium Metric Card Styling */
    div[data-testid="stMetric"] {
        background: linear-gradient(145deg, #111827 0%, #0f172a 100%);
        border: 1px solid #1e293b;
        padding: 16px;
        border-radius: 14px;
        box-shadow: 0 4px 15px -3px rgba(0, 0, 0, 0.5);
        transition: transform 0.2s ease, border-color 0.2s ease;
    }
    div[data-testid="stMetric"]:hover {
        border-color: #3b82f6;
        transform: translateY(-2px);
    }
    div[data-testid="stMetric"] label {
        color: #94a3b8 !important;
        font-weight: 600;
        font-size: 0.9rem !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #f8fafc !important;
        font-weight: 800;
        font-size: 1.5rem !important;
    }

    /* Tabs Styling Polish */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #111827;
        padding: 6px;
        border-radius: 12px;
        border: 1px solid #1f2937;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        color: #9ca3af;
        font-weight: 600;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%) !important;
        color: white !important;
    }
</style>
""", unsafe_allow_html=True)

# Render Branded Header with Logo
st.markdown("""
<div class="brand-header">
    <div class="brand-logo">🐂⚡</div>
    <div>
        <div class="brand-title">BIG MAN — Forex AI Pro (High Yield Bid Enabled)</div>
        <div class="brand-subtitle">Institutional-Grade Paper Trading & Quantitative Ensemble Dashboard (IST Enabled)</div>
    </div>
</div>
""", unsafe_allow_html=True)

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


def convert_utc_to_ist(utc_str):
    """Converts UTC timestamp string to Indian Standard Time (IST) format."""
    if not utc_str or pd.isna(utc_str):
        return ""
    try:
        dt = pd.to_datetime(utc_str)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        ist_zone = timezone(timedelta(hours=5, minutes=30))
        ist_dt = dt.tz_convert(ist_zone)
        return ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return str(utc_str)


def db_connect():
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
            return pd.DataFrame(), f"Yahoo Finance ne {ticker} ke liye data nahi diya."
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
        return pd.DataFrame(), f"Market data fetch error: {type(exc).__name__}: {exc}"


def fetch_fresh_exit_candle(symbol):
    """Fetch the latest valid market price specifically for time-based trade exits. Never falls back to entry price."""
    try:
        raw = yf.download(symbol, interval="1m", period="1d", auto_adjust=False, progress=False, threads=False)
        if raw is None or raw.empty:
            raw = yf.download(symbol, interval="15m", period="5d", auto_adjust=False, progress=False, threads=False)
        if raw is None or raw.empty:
            return None, "Fresh market data unavailable"
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).strip() for c in raw.columns]
        if not {"Open", "High", "Low", "Close"}.issubset(raw.columns):
            return None, "Missing price columns"
        data = raw.reset_index()
        time_col = "Datetime" if "Datetime" in data.columns else "Date"
        if time_col not in data.columns:
            time_col = data.columns[0]
        data = data.rename(columns={time_col: "time", "Close": "close", "High": "high", "Low": "low", "Open": "open"})
        data["time"] = pd.to_datetime(data["time"], errors="coerce", utc=True)
        data = data.dropna(subset=["time", "close"]).sort_values("time")
        if data.empty:
            return None, "No valid price records"
        return data.iloc[-1], None
    except Exception as exc:
        return None, str(exc)


def weighted_strategy_consensus(votes_df):
    """Computes weighted ensemble score and net market bias based on strategy reliability."""
    weights = {
        "Support / Resistance": 1.5,
        "Trend Alignment": 1.5,
        "Fake Breakout (proxy)": 1.5,
        "Two-Candle Price Action": 1.0,
        "Fibonacci Zone (proxy)": 1.0,
        "Breakout": 1.0,
        "SMA Touch / Bounce (proxy)": 0.75,
        "Range Supply / Demand (proxy)": 0.75,
    }
    df = votes_df.copy()
    df["Weight"] = df["Strategy"].map(weights).fillna(1.0)

    buy_score = df[df["Signal"] == "BUY"]["Weight"].sum()
    sell_score = df[df["Signal"] == "SELL"]["Weight"].sum()
    total_weight = df["Weight"].sum()

    net_consensus = ((sell_score - buy_score) / total_weight) * 100 if total_weight > 0 else 0.0
    return buy_score, sell_score, net_consensus


def strategy_signals(data):
    """Original BIG MAN2 8-Strategy Ensemble Voting System."""
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
    """Evaluate open paper trades against SL/TP or robust 15-minute Time Exit without fallback to entry prices."""
    trades = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' AND symbol=?", (symbol,)).fetchall()
    closed = 0
    hi, lo = float(last_candle["high"]), float(last_candle["low"])
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

        elapsed_minutes = (now_utc - created_time.to_pydatetime()).total_seconds() / 60.0
        if exit_price is None and elapsed_minutes >= 15.0:
            fresh_candle, err = fetch_fresh_exit_candle(trade["symbol"])
            if fresh_candle is not None and "close" in fresh_candle:
                exit_price = float(fresh_candle["close"])
                reason = "Time Exit"
            else:
                st.warning(f"Trade #{trade['id']} ({trade['symbol']}) reached 15-minute duration, but fresh market data is unavailable ({err}). Keeping trade open.")
                continue

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
    open_count = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
    if open_count >= 3:
        return False, "Maximum 3 open paper trades ki limit poori ho chuki hai."
    
    entry = float(last_candle["close"])
    stop_distance = max(float(atr) * 1.5, entry * 0.001)
    
    # INCREASED BID / PROFIT TARGET (1:3 Risk-to-Reward Ratio)
    stop = entry - stop_distance if side == "BUY" else entry + stop_distance
    target = entry + stop_distance * 3.0 if side == "BUY" else entry - stop_distance * 3.0
    
    # INCREASED RISK & QUANTITY (1.5% Risk, up to 100,000 units lot size for bigger $ returns)
    risk_cash = max(10.0, float(balance) * 0.015)
    qty = min(risk_cash / stop_distance, 100000.0)
    
    note = ", ".join(signals.loc[signals["Signal"] == side, "Strategy"].tolist())
    
    con.execute("""INSERT INTO paper_trades(created_at,symbol,timeframe,side,entry,stop,target,quantity,status,strategy_note,balance_updated)
                  VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?, 0)""", (utc_now_str(), symbol, timeframe, side, entry, stop, target, qty, note))
    return True, f"High-Bid paper {side} trade created (1:3 R:R, Bada Profit Target)."


def background_monitor_worker():
    while True:
        time.sleep(60)
        try:
            with db_connect() as con:
                open_trades = con.execute("SELECT DISTINCT symbol FROM paper_trades WHERE status='OPEN'").fetchall()
                for ot in open_trades:
                    sym = ot["symbol"]
                    raw, err = fetch_candles(sym, "15m", "1d")
                    if not raw.empty:
                        evaluate_and_close_trades(con, sym, raw.iloc[-1])
        except Exception:
            pass


@st.cache_resource
def start_background_monitor():
    t = threading.Thread(target=background_monitor_worker, daemon=True)
    t.start()
    return True


init_db()
start_background_monitor()

with st.sidebar:
    st.header("Settings")
    pair_label = st.selectbox("Currency pair", list(PAIRS.keys()))
    timeframe_label = st.selectbox("Timeframe", list(TIMEFRAMES.keys()))
    signal_mode = st.selectbox(
        "Signal mode",
        ["Safe Mode", "Fast Mode"],
        index=0,
        help="Safe Mode: 5/8 votes. Fast Mode: 3/8 votes.",
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
    new_auto = st.toggle("Auto paper trading", value=auto_enabled, help="Creates simulated trades when app runs/refreshes.")
    if new_auto != auto_enabled:
        set_setting("auto_paper", "1" if new_auto else "0")
        st.rerun()
    st.button("🔄 Refresh market data", on_click=st.cache_data.clear, use_container_width=True)
    st.caption("Streamlit Cloud hosting note: Background monitoring runs while process is active.")

balance = float(get_setting("virtual_balance", "10000") or 10000)
data, fetch_error = fetch_candles(ticker, interval, period)

analyzer_tab, library_tab, paper_tab, setup_tab = st.tabs(["📊 Analyzer", "🧠 Strategy Library", "🧾 Paper Trading", "⚙️ Setup & Limits"])

with analyzer_tab:
    st.subheader(f"{pair_label} · {timeframe_label}")
    
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
        st.info("Internet connection check karein, phir Refresh market data dabayein.")
    elif len(data) < 3:
        st.warning("Signal banane ke liye abhi candles kam hain.")
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
                        st.info("Is candle par demo trade pehle hi create ho chuka hai.")
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
                st.caption("Auto Paper Trading OFF hai.")
                
            if closed_count:
                st.success(f"{closed_count} paper trade(s) closed based on SL/TP or verified 15-minute Time Exit.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Combined signal", signal)
        c2.metric("BUY votes", f"{buy_votes}/8")
        c3.metric("SELL votes", f"{sell_votes}/8")
        c4.metric("Latest close", f"{float(last['close']):.5f}")
        st.caption(f"Mode: {signal_mode} · Signal threshold: {vote_threshold}/8 votes. High-Bid 1:3 R:R execution enabled.")

        fig = go.Figure(data=[go.Candlestick(x=analyzed["time"], open=analyzed["open"], high=analyzed["high"], low=analyzed["low"], close=analyzed["close"], name="Candles")])
        fig.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma20"], name="SMA 20", mode="lines"))
        fig.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma50"], name="SMA 50", mode="lines"))
        fig.update_layout(
            height=520, 
            xaxis_rangeslider_visible=False, 
            margin=dict(l=10, r=10, t=30, b=10),
            paper_bgcolor="#0b0f19",
            plot_bgcolor="#0b0f19",
            font=dict(color="#f3f4f6")
        )
        st.plotly_chart(fig, use_container_width=True)

        buy_score, sell_score, net_consensus = weighted_strategy_consensus(votes)
        st.markdown("### 📈 Ensemble Weighted Consensus Meter")
        wc1, wc2, wc3 = st.columns(3)
        wc1.metric("Bearish Weight", f"{sell_score:.1f} pts")
        wc2.metric("Bullish Weight", f"{buy_score:.1f} pts")
        wc3.metric("Net Market Bias", f"{net_consensus:+.1f}%", delta="Bearish Tilt" if net_consensus > 0 else "Bullish Tilt")

        def color_signals(val):
            if val == "BUY":
                return "color: #2ecc71; font-weight: bold;"
            elif val == "SELL":
                return "color: #e74c3c; font-weight: bold;"
            elif val == "WAIT":
                return "color: #f1c40f; font-weight: bold;"
            return ""

        st.subheader("8 Strategy Votes & Ensemble Breakdown")
        st.dataframe(votes.style.map(color_signals, subset=["Signal"]), use_container_width=True, hide_index=True)

with library_tab:
    st.subheader("Strategy Library (BIG MAN2 Original 8 Strategies)")
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

with paper_tab:
    st.subheader("Paper Trading Journal & Balance Accounting (IST Timings)")
    
    current_balance_val = float(get_setting("virtual_balance", "10000") or 10000)
    
    with db_connect() as journal_con:
        trades = pd.read_sql_query("""
            SELECT id AS 'Trade ID', 
                   symbol AS 'Currency Pair', 
                   side AS 'Side', 
                   entry AS 'Entry Price', 
                   created_at AS 'Entry Time (UTC)', 
                   stop AS 'Stop Loss', 
                   target AS 'Take Profit', 
                   exit_price AS 'Exit Price', 
                   exit_time AS 'Exit Time (UTC)', 
                   duration AS 'Duration', 
                   status AS 'Status', 
                   pnl AS 'P/L ($)', 
                   exit_reason AS 'Exit Reason' 
            FROM paper_trades 
            ORDER BY id DESC
        """, journal_con)

    win_rate, profit_factor = 0.0, 0.0
    if not trades.empty:
        trades['Entry Time'] = trades['Entry Time (UTC)'].apply(convert_utc_to_ist)
        trades['Exit Time'] = trades['Exit Time (UTC)'].apply(convert_utc_to_ist)
        
        display_trades = trades.drop(columns=['Entry Time (UTC)', 'Exit Time (UTC)'])
        cols = ['Trade ID', 'Currency Pair', 'Side', 'Entry Price', 'Entry Time', 'Stop Loss', 'Take Profit', 'Exit Price', 'Exit Time', 'Duration', 'Status', 'P/L ($)', 'Exit Reason']
        existing_cols = [c for c in cols if c in display_trades.columns]
        display_trades = display_trades[existing_cols]

        closed_trades = trades[trades["Status"] == "CLOSED"].copy()
        closed_trades["P/L ($)"] = pd.to_numeric(closed_trades["P/L ($)"], errors="coerce").fillna(0)
        total_realized_pnl = closed_trades["P/L ($)"].sum()

        if len(closed_trades) > 0:
            wins = closed_trades[closed_trades["P/L ($)"] > 0]
            losses = closed_trades[closed_trades["P/L ($)"] < 0]
            win_rate = (len(wins) / len(closed_trades)) * 100

            gross_wins = wins["P/L ($)"].sum()
            gross_losses = abs(losses["P/L ($)"].sum())
            profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)
    else:
        display_trades = trades
        total_realized_pnl = 0.0

    bm1, bm2, bm3, bm4, bm5, bm6 = st.columns(6)
    bm1.metric("Virtual Balance", f"${current_balance_val:,.2f}")
    bm2.metric("Realized P/L", f"${total_realized_pnl:,.2f}")
    bm3.metric("Win Rate", f"{win_rate:.1f}%")
    bm4.metric("Profit Factor", f"{profit_factor:.2f}")
    bm5.metric("Total Trades", len(trades))
    bm6.metric("Open Trades", int((trades["Status"] == "OPEN").sum()) if not trades.empty else 0)

    if display_trades.empty:
        st.info("Abhi koi paper trade nahi hai.")
    else:
        st.dataframe(display_trades, use_container_width=True, hide_index=True)
        st.download_button("Download trade journal CSV", display_trades.to_csv(index=False).encode("utf-8"), file_name="big_man_paper_trades_ist.csv", mime="text/css")
    st.caption("Accounting note: Trade entry & exit timestamps are automatically displayed in Indian Standard Time (IST).")

with setup_tab:
    st.subheader("Setup & Important Limits")
    st.markdown("""
    - **High-Bid Position Sizing:** Lot size scaled up (up to 100,000 units) with 1.5% risk to target meaningful profits ($15–$50+).
    - **Pro Risk Management:** Configured with an expanded 1:3 Risk-to-Reward ratio for higher profit targets.
    - **Original Strategies Integrated:** Retains the proven 8-strategy voting ensemble.
    - **Simultaneous Trades Limit:** Maximum **3 open paper trades**.
    - **IST Timestamps & UI:** Premium dark mode styling with Indian Standard Time conversion.
    """)
    if st.button("Clear cached market data"):
        st.cache_data.clear()
        st.success("Cache cleared. Page refresh karein.")
