import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

# BIG MAN2 Forex AI Pro
# Paper-trading only. This version does not send real broker orders.

st.set_page_config(
    page_title="BIG MAN2 — Forex AI Pro",
    page_icon="📈",
    layout="wide",
)

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
try:
    DATA_DIR.mkdir(exist_ok=True)
    probe = DATA_DIR / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)
    DB_PATH = DATA_DIR / "big_man2.db"
except OSError:
    DB_PATH = Path("/tmp") / "big_man2.db"

PAIRS = {
    "EUR/USD": {"yahoo": "EURUSD=X", "twelve": "EUR/USD"},
    "GBP/USD": {"yahoo": "GBPUSD=X", "twelve": "GBP/USD"},
    "USD/JPY": {"yahoo": "JPY=X", "twelve": "USD/JPY"},
}
TIMEFRAMES = {
    "H1 (1 hour)": {"twelve": "1h", "yahoo": "60m", "period": "1mo", "code": "H1"},
    "M15 (15 minutes)": {"twelve": "15min", "yahoo": "15m", "period": "5d", "code": "M15"},
    "D1 (1 day)": {"twelve": "1day", "yahoo": "1d", "period": "6mo", "code": "D1"},
}

st.markdown("""
<style>
.stApp { background:#0b0f19; color:#f3f4f6; }
.brand { padding:14px 0 20px; border-bottom:1px solid #263244; margin-bottom:18px; }
.brand h1 { color:#fff; font-size:1.8rem; margin:0; }
.brand p { color:#9ca3af; margin:4px 0 0; }
div[data-testid="stMetric"] { background:#111827; border:1px solid #263244; padding:12px; border-radius:12px; }
</style>
""", unsafe_allow_html=True)
st.markdown(
    '<div class="brand"><h1>🐂⚡ BIG MAN2 — Forex AI Pro</h1>'
    '<p>SMC & Price Action • Market analysis • Paper trading only</p></div>',
    unsafe_allow_html=True,
)

def utc_now_str():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def convert_utc_to_ist(value):
    if not value or pd.isna(value):
        return ""
    try:
        dt = pd.to_datetime(value, utc=True)
        return dt.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return str(value)

def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
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
            exit_time TEXT,
            duration TEXT,
            pnl REAL,
            exit_reason TEXT,
            strategy_note TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        defaults = {"virtual_balance": "10000", "auto_paper": "0"}
        for key, value in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)",
                (key, value),
            )

def get_setting(key, default=""):
    try:
        with db_connect() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default

def set_setting(key, value):
    with db_connect() as con:
        con.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

def fetch_twelve_data(pair_label, interval, outputsize=500):
    """Fetch OHLC candles from Twelve Data using Streamlit Secrets."""
    try:
        api_key = st.secrets.get("TWELVE_DATA_API_KEY", "")
    except Exception:
        api_key = ""
    if not api_key or api_key == "YOUR_NEW_API_KEY":
        return pd.DataFrame(), "Twelve Data API key missing in Streamlit Secrets."

    params = {
        "symbol": PAIRS[pair_label]["twelve"],
        "interval": interval,
        "outputsize": min(int(outputsize), 5000),
        "apikey": api_key,
        "format": "JSON",
        "timezone": "UTC",
    }
    try:
        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if "values" not in result:
            return pd.DataFrame(), str(result.get("message", result.get("code", "No data returned")))
        df = pd.DataFrame(result["values"])
        required = ["datetime", "open", "high", "low", "close"]
        if not all(c in df.columns for c in required):
            return pd.DataFrame(), "Twelve Data returned incomplete candle data."
        df["time"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df else 0
        df = df[["time", "open", "high", "low", "close", "volume"]]
        df = df.dropna(subset=["time", "open", "high", "low", "close"])
        df = df.sort_values("time").tail(outputsize).reset_index(drop=True)
        if df.empty:
            return pd.DataFrame(), "No valid candles returned by Twelve Data."
        return df, None
    except requests.RequestException as exc:
        return pd.DataFrame(), f"Twelve Data network error: {type(exc).__name__}"
    except Exception as exc:
        return pd.DataFrame(), f"Twelve Data error: {type(exc).__name__}"

def fetch_yahoo_data(pair_label, yahoo_interval, period):
    try:
        raw = yf.download(
            PAIRS[pair_label]["yahoo"],
            interval=yahoo_interval,
            period=period,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty:
            return pd.DataFrame(), "Yahoo Finance returned no data."
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).strip() for c in raw.columns]
        raw = raw.reset_index()
        time_col = "Datetime" if "Datetime" in raw.columns else "Date"
        if time_col not in raw.columns:
            time_col = raw.columns[0]
        raw = raw.rename(columns={
            time_col: "time", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
        raw["time"] = pd.to_datetime(raw["time"], errors="coerce", utc=True)
        for col in ["open", "high", "low", "close"]:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        if "volume" not in raw:
            raw["volume"] = 0
        raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce").fillna(0)
        raw = raw[["time", "open", "high", "low", "close", "volume"]]
        raw = raw.dropna(subset=["time", "open", "high", "low", "close"])
        raw = raw.sort_values("time").tail(500).reset_index(drop=True)
        if raw.empty:
            return pd.DataFrame(), "Yahoo Finance returned no valid candles."
        return raw, None
    except Exception as exc:
        return pd.DataFrame(), f"Yahoo Finance error: {type(exc).__name__}"

def fetch_candles(pair_label, tf_info):
    td_df, td_error = fetch_twelve_data(pair_label, tf_info["twelve"], 500)
    if not td_df.empty:
        return td_df, None, "Twelve Data"
    yahoo_df, yahoo_error = fetch_yahoo_data(
        pair_label, tf_info["yahoo"], tf_info["period"]
    )
    if not yahoo_df.empty:
        return yahoo_df, None, "Yahoo Finance fallback"
    return pd.DataFrame(), f"Twelve Data: {td_error} | Yahoo: {yahoo_error}", "Unavailable"

def strategy_signals(data):
    df = data.copy().sort_values("time").reset_index(drop=True)
    df["sma20"] = df["close"].rolling(20, min_periods=5).mean()
    df["sma50"] = df["close"].rolling(50, min_periods=10).mean()
    df["prior_high"] = df["high"].rolling(20, min_periods=5).max().shift(1)
    df["prior_low"] = df["low"].rolling(20, min_periods=5).min().shift(1)
    df["range"] = (df["high"] - df["low"]).abs()
    df["atr14"] = df["range"].rolling(14, min_periods=5).mean()
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    prev2 = df.iloc[-3] if len(df) > 2 else prev
    close = float(last["close"])
    atr = float(last["atr14"]) if pd.notna(last["atr14"]) else close * 0.001
    atr = max(atr, close * 0.0001)
    sma20 = float(last["sma20"]) if pd.notna(last["sma20"]) else close
    sma50 = float(last["sma50"]) if pd.notna(last["sma50"]) else close
    prior_high = float(last["prior_high"]) if pd.notna(last["prior_high"]) else float(df["high"].iloc[:-1].max())
    prior_low = float(last["prior_low"]) if pd.notna(last["prior_low"]) else float(df["low"].iloc[:-1].min())
    width = max(prior_high - prior_low, close * 0.00001)
    signals = []

    def add(name, vote, note):
        signals.append({"Strategy": name, "Signal": vote, "Reason": note})

    add("Support / Resistance",
        "BUY" if close > prior_low + width * 0.65 else "SELL" if close < prior_low + width * 0.35 else "WAIT",
        "Price position inside recent 20-candle range")
    fib_low = prior_low + width * 0.382
    fib_high = prior_low + width * 0.618
    equilibrium = (prior_high + prior_low) / 2
    add("Fibonacci & Equilibrium (50%)",
        "BUY" if close > equilibrium and close >= fib_low else "SELL" if close < equilibrium and close <= fib_high else "WAIT",
        f"Equilibrium midpoint {equilibrium:.5f}")
    add("Breakout",
        "BUY" if close > prior_high else "SELL" if close < prior_low else "WAIT",
        "Close compared with previous range")
    prev_close = float(prev["close"])
    broke_up = prev_close > prior_high
    broke_dn = prev_close < prior_low
    add("SMC Inducement (IDM) Trap",
        "SELL" if broke_up and close < prior_high else "BUY" if broke_dn and close > prior_low else "WAIT",
        "Possible liquidity sweep / trap; heuristic only")
    add("Trend Alignment",
        "BUY" if sma20 > sma50 and close > sma20 else "SELL" if sma20 < sma50 and close < sma20 else "WAIT",
        "SMA20/SMA50 trend filter")
    is_bull_engulf = close > prev_close and close > float(last["open"]) and prev_close < float(prev["open"])
    is_bear_engulf = close < prev_close and close < float(last["open"]) and prev_close > float(prev["open"])
    add("Two-Candle Price Action",
        "BUY" if is_bull_engulf else "SELL" if is_bear_engulf else "WAIT",
        "Engulfing-style candle check")
    bullish_fvg = float(prev2["high"]) < float(last["low"])
    bearish_fvg = float(prev2["low"]) > float(last["high"])
    add("Fair Value Gap (FVG)",
        "BUY" if bullish_fvg else "SELL" if bearish_fvg else "WAIT",
        "Simple three-candle gap heuristic")
    add("Supply & Demand Zones",
        "BUY" if close < prior_low + width * 0.25 else "SELL" if close > prior_low + width * 0.75 else "WAIT",
        "Range-extreme heuristic")
    return df, pd.DataFrame(signals), atr

def aggregate_signal(votes, threshold):
    buys = int((votes["Signal"] == "BUY").sum())
    sells = int((votes["Signal"] == "SELL").sum())
    if buys >= threshold and sells < threshold:
        return "BUY", buys, sells
    if sells >= threshold and buys < threshold:
        return "SELL", buys, sells
    return "WAIT", buys, sells

def create_paper_trade(pair, timeframe, side, last, atr, votes=None):
    with db_connect() as con:
        open_count = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
        if open_count >= 3:
            return False, "Maximum 3 open paper trades allowed."
        entry = float(last["close"])
        stop_distance = max(float(atr) * 1.5, entry * 0.001)
        stop = entry - stop_distance if side == "BUY" else entry + stop_distance
        target = entry + stop_distance * 3 if side == "BUY" else entry - stop_distance * 3
        balance = float(get_setting("virtual_balance", "10000") or 10000)
        risk_cash = max(1.0, balance * 0.01)
        quantity = min(risk_cash / stop_distance, 100000.0)
        note = ", ".join(votes.loc[votes["Signal"] == side, "Strategy"].tolist()) if votes is not None else "Manual paper trade"
        con.execute(
            """INSERT INTO paper_trades
            (created_at,symbol,timeframe,side,entry,stop,target,quantity,status,strategy_note)
            VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?)""",
            (utc_now_str(), pair, timeframe, side, entry, stop, target, quantity, note),
        )
    return True, f"Paper trade created at {entry:.5f}; SL {stop:.5f}; TP {target:.5f}."

def close_paper_trade(trade_id, exit_price, reason="Manual Exit"):
    with db_connect() as con:
        trade = con.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not trade or trade["status"] != "OPEN":
            return False, "Trade not found or already closed."
        entry, qty = float(trade["entry"]), float(trade["quantity"])
        pnl = (exit_price - entry) * qty if trade["side"] == "BUY" else (entry - exit_price) * qty
        created = pd.to_datetime(trade["created_at"], utc=True)
        duration_seconds = max(0, int((datetime.now(timezone.utc) - created.to_pydatetime()).total_seconds()))
        duration = f"{duration_seconds // 60}m {duration_seconds % 60}s"
        balance = float(get_setting("virtual_balance", "10000") or 10000)
        set_setting("virtual_balance", balance + pnl)
        con.execute(
            """UPDATE paper_trades SET status='CLOSED',exit_price=?,exit_time=?,
            duration=?,pnl=?,exit_reason=? WHERE id=?""",
            (float(exit_price), utc_now_str(), duration, pnl, reason, trade_id),
        )
    return True, f"Paper trade #{trade_id} closed. Simulated P/L: ${pnl:.2f}"

def auto_evaluate_open_trades(pair, data):
    if data.empty:
        return
    candle = data.iloc[-1]
    high, low = float(candle["high"]), float(candle["low"])
    to_close = []
    with db_connect() as con:
        trades = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' AND symbol=?", (pair,)).fetchall()
        for trade in trades:
            side = trade["side"]
            stop, target = float(trade["stop"]), float(trade["target"])
            exit_price, reason = None, None
            if side == "BUY":
                if low <= stop:
                    exit_price, reason = stop, "Stop Loss"
                elif high >= target:
                    exit_price, reason = target, "Take Profit"
            else:
                if high >= stop:
                    exit_price, reason = stop, "Stop Loss"
                elif low <= target:
                    exit_price, reason = target, "Take Profit"
            if exit_price is not None:
                to_close.append((trade["id"], exit_price, reason))
    for trade_id, price, reason in to_close:
        close_paper_trade(trade_id, price, reason)

init_db()

with st.sidebar:
    st.header("Settings")
    pair_label = st.selectbox("Currency pair", list(PAIRS.keys()))
    signal_mode = st.selectbox("Signal mode", ["Safe Mode", "Fast Mode"])
    threshold = 5 if signal_mode == "Safe Mode" else 3
    current_balance = float(get_setting("virtual_balance", "10000") or 10000)
    balance_input = st.number_input("Demo balance ($)", min_value=100.0, max_value=1_000_000.0, value=current_balance, step=100.0)
    if st.button("Save demo balance", use_container_width=True):
        set_setting("virtual_balance", balance_input)
        st.success("Demo balance saved.")
    auto_setting = get_setting("auto_paper", "0") == "1"
    auto_toggle = st.toggle("Auto paper trading", value=auto_setting, help="Creates simulated trades when a signal qualifies. No real orders are sent.")
    if auto_toggle != auto_setting:
        set_setting("auto_paper", "1" if auto_toggle else "0")
        st.rerun()
    st.caption("Real-money / MT5 order execution is not included in this safe version.")
    if st.button("🔄 Refresh market data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

balance = float(get_setting("virtual_balance", "10000") or 10000)
analyzer_tab, library_tab, journal_tab, setup_tab = st.tabs(
    ["📊 Analyzer", "🧠 Strategy Library", "🧾 Paper Journal", "⚙️ Setup"]
)

with analyzer_tab:
    st.subheader(f"{pair_label} — Multi-timeframe analysis")
    tf_tabs = st.tabs(list(TIMEFRAMES.keys()))
    for idx, (tf_name, tf_info) in enumerate(TIMEFRAMES.items()):
        with tf_tabs[idx]:
            data, fetch_error, source = fetch_candles(pair_label, tf_info)
            if fetch_error or data.empty:
                st.error(fetch_error or "Market data unavailable.")
                st.info("Check the API key in Streamlit Secrets and verify your Twelve Data plan/credits. Yahoo Finance fallback may also be temporarily unavailable.")
                continue
            st.caption(f"Data source: {source} • Latest candle time: {convert_utc_to_ist(data.iloc[-1]['time'])}")
            if len(data) < 3:
                st.warning("Not enough candles to calculate strategy signals.")
                continue
            analyzed, votes, atr = strategy_signals(data)
            last = analyzed.iloc[-1]
            signal, buys, sells = aggregate_signal(votes, threshold)
            auto_now = get_setting("auto_paper", "0") == "1"
            if auto_now and signal in ("BUY", "SELL"):
                candle_key = f"auto_done_{pair_label}_{tf_info['code']}"
                candle_time = pd.Timestamp(last["time"]).isoformat()
                if get_setting(candle_key, "") != candle_time:
                    made, message = create_paper_trade(pair_label, tf_info["code"], signal, last, atr, votes)
                    if made:
                        set_setting(candle_key, candle_time)
                        st.success("Auto paper trade: " + message)
                    else:
                        st.warning(message)
            auto_evaluate_open_trades(pair_label, data)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric(f"Signal — {tf_info['code']}", signal)
            m2.metric("BUY votes", f"{buys}/8")
            m3.metric("SELL votes", f"{sells}/8")
            m4.metric("Latest close", f"{float(last['close']):.5f}")

            chart = go.Figure()
            chart.add_trace(go.Candlestick(
                x=analyzed["time"], open=analyzed["open"], high=analyzed["high"],
                low=analyzed["low"], close=analyzed["close"], name=pair_label,
            ))
            chart.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma20"], name="SMA 20", mode="lines"))
            chart.add_trace(go.Scatter(x=analyzed["time"], y=analyzed["sma50"], name="SMA 50", mode="lines"))
            chart.update_layout(
                height=440, xaxis_rangeslider_visible=False,
                margin=dict(l=10, r=10, t=25, b=10),
                paper_bgcolor="#0b0f19", plot_bgcolor="#0b0f19",
                font=dict(color="#f3f4f6"),
            )
            st.plotly_chart(chart, use_container_width=True)

            st.subheader("8 SMC & Price Action strategy votes")
            st.dataframe(votes, use_container_width=True, hide_index=True)

            with st.expander(f"Manual paper trade — {tf_info['code']}"):
                direction = st.selectbox("Direction", ["BUY", "SELL"], key=f"direction_{tf_info['code']}")
                if st.button("Create paper trade", key=f"create_{tf_info['code']}", use_container_width=True):
                    ok, msg = create_paper_trade(pair_label, tf_info["code"], direction, last, atr, votes)
                    (st.success if ok else st.error)(msg)

with library_tab:
    st.subheader("Strategy Library")
    st.dataframe(pd.DataFrame([
        {"Strategy": "Support / Resistance", "Purpose": "Recent range structural levels"},
        {"Strategy": "Fibonacci & Equilibrium (50%)", "Purpose": "Range midpoint and discount/premium heuristic"},
        {"Strategy": "Breakout", "Purpose": "Previous range high/low break"},
        {"Strategy": "SMC Inducement (IDM) Trap", "Purpose": "Possible liquidity sweep heuristic"},
        {"Strategy": "Trend Alignment", "Purpose": "SMA20/SMA50 trend filter"},
        {"Strategy": "Two-Candle Price Action", "Purpose": "Engulfing-style candle confirmation"},
        {"Strategy": "Fair Value Gap (FVG)", "Purpose": "Three-candle gap heuristic"},
        {"Strategy": "Supply & Demand Zones", "Purpose": "Range-extreme heuristic"},
    ]), use_container_width=True, hide_index=True)
    st.caption("These are rule-based heuristics, not a guarantee of institutional order flow or profitable trades.")

with journal_tab:
    st.subheader("Active Open Trades — Manual Management")
    with db_connect() as con:
        open_df = pd.read_sql_query(
            "SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY id DESC", con
        )
    if open_df.empty:
        st.info("No open paper trades.")
    else:
        for _, row in open_df.iterrows():
            c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
            c1.write(f"**#{row['id']}** {row['symbol']} [{row['timeframe']}] {row['side']}")
            c2.write(f"Entry: `{row['entry']:.5f}`")
            c3.write(f"SL: `{row['stop']:.5f}` · TP: `{row['target']:.5f}`")
            if c4.button("Close trade", key=f"close_{row['id']}"):
                # Use latest available close when possible; otherwise entry is a neutral fallback.
                exit_price = float(row["entry"])
                close_data, _, _ = fetch_candles(row["symbol"], TIMEFRAMES["M15 (15 minutes)"])
                if not close_data.empty:
                    exit_price = float(close_data.iloc[-1]["close"])
                ok, msg = close_paper_trade(int(row["id"]), exit_price)
                (st.success if ok else st.error)(msg)
                st.rerun()

    st.markdown("---")
    st.subheader("Paper trade journal (IST)")
    with db_connect() as con:
        trades = pd.read_sql_query("SELECT * FROM paper_trades ORDER BY id DESC", con)
    if trades.empty:
        st.info("No paper trades recorded yet.")
    else:
        trades["Entry Time (IST)"] = trades["created_at"].apply(convert_utc_to_ist)
        trades["Exit Time (IST)"] = trades["exit_time"].apply(convert_utc_to_ist)
        closed = trades[trades["status"] == "CLOSED"]
        pnl_series = pd.to_numeric(closed["pnl"], errors="coerce").fillna(0)
        wins = int((pnl_series > 0).sum())
        win_rate = (wins / len(closed) * 100) if len(closed) else 0.0
        total_pnl = float(pnl_series.sum()) if len(closed) else 0.0
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Demo balance", f"${float(get_setting('virtual_balance', '10000')):,.2f}")
        c2.metric("Realized simulated P/L", f"${total_pnl:,.2f}")
        c3.metric("Win rate", f"{win_rate:.1f}%")
        c4.metric("Total trades", len(trades))
        display_cols = [
            "id", "symbol", "timeframe", "side", "entry", "Entry Time (IST)",
            "stop", "target", "exit_price", "Exit Time (IST)", "duration",
            "status", "pnl", "exit_reason", "strategy_note",
        ]
        st.dataframe(trades[display_cols], use_container_width=True, hide_index=True)
        st.download_button(
            "Download paper journal CSV",
            trades[display_cols].to_csv(index=False).encode("utf-8"),
            file_name="big_man2_paper_journal.csv",
            mime="text/csv",
        )

with setup_tab:
    st.subheader("Setup & limits")
    st.markdown("""
- Add your API key under Streamlit Community Cloud → App → Settings → Secrets:
  `TWELVE_DATA_API_KEY = "your_actual_key"`
- Twelve Data is tried first; Yahoo Finance is a fallback, not guaranteed real-time.
- Data availability, intervals, and request limits depend on your API plan.
- Signals are rule-based technical indicators, not financial advice or guaranteed predictions.
- This version is paper-trading only and does not send MT5 or broker orders.
- The SQLite database may reset or be ephemeral on some hosted deployments; export the journal CSV periodically.
""")
