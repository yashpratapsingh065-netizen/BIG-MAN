import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import yfinance as yf

APP_DIR = Path(__file__).parent
# Streamlit Cloud source directory may be read-only; /tmp is writable but ephemeral.
# Prefer a persistent data folder when writable; fall back to /tmp on read-only hosts.
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
            "last_trade_candles": "{}",
        }
        for key, value in defaults.items():
            existing = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if not existing:
                con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, value))
                
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
        try:
            old = con.execute("SELECT value FROM app_settings WHERE key='last_processed_candle'").fetchone()
            new = con.execute("SELECT value FROM app_settings WHERE key='last_processed_candles'").fetchone()
            if old and new and new[0] == "{}" and old[0]:
                con.execute("UPDATE app_settings SET value=? WHERE key='last_processed_candles'", (json.dumps({"legacy": old[0]}),))
        except Exception:
            pass


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
        # Normalize provider column labels safely after flattening MultiIndex.
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
    candle_time = pd.Timestamp(last_candle["time"])
    for trade in trades:
        # A trade created at this candle's close must not be exited using that
        # same candle's earlier high/low (look-ahead bias). Wait for a later candle.
        try:
            created_time = pd.Timestamp(trade["created_at"])
            if created_time.tzinfo is None:
                created_time = created_time.tz_localize("UTC")
            if created_time >= candle_time:
                continue
        except (TypeError, ValueError):
            continue
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
    stop_distance = max(float(atr) * 1.5, entry * 0.001
