import json
from pathlib import Path
from datetime import datetime, timezone
import sqlite3

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import yfinance as yf

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "big_man.db"
STRATEGIES_PATH = APP_DIR / "strategies.json"

st.set_page_config(page_title="BIG MAN Trading Lab", page_icon="🤖", layout="wide")


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        entry REAL NOT NULL,
        stop REAL NOT NULL,
        target REAL NOT NULL,
        quantity REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        exit_price REAL,
        pnl REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('virtual_balance','10000')")
    conn.commit()
    return conn


def load_strategies():
    return json.loads(STRATEGIES_PATH.read_text(encoding="utf-8"))


@st.cache_data(ttl=300, show_spinner=False)
def load_forex_data(ticker, interval, period):
    """Fetch recent candles from Yahoo Finance; availability and delay can vary."""
    frame = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    needed = ["Open", "High", "Low", "Close"]
    if not all(col in frame.columns for col in needed):
        return pd.DataFrame()
    frame = frame.reset_index()
    time_col = "Datetime" if "Datetime" in frame.columns else "Date"
    if time_col not in frame.columns:
        time_col = frame.columns[0]
    result = pd.DataFrame({
        "time": pd.to_datetime(frame[time_col], errors="coerce"),
        "open": pd.to_numeric(frame["Open"], errors="coerce"),
        "high": pd.to_numeric(frame["High"], errors="coerce"),
        "low": pd.to_numeric(frame["Low"], errors="coerce"),
        "close": pd.to_numeric(frame["Close"], errors="coerce"),
    })
    result = result.dropna(subset=["time", "open", "high", "low", "close"])
    return result.tail(500).reset_index(drop=True)


def load_trades(conn):
    return pd.read_sql_query("SELECT * FROM paper_trades ORDER BY id DESC", conn)


conn = db()
st.title("🤖 BIG MAN — Trading Lab")
st.caption("Version 1.1 • Forex market-data preview + draft strategy library + paper-trade journal")
st.warning(
    "Educational prototype only. Yahoo Finance data can be delayed, incomplete, or unavailable. "
    "This app does not send orders to a broker. Never treat its test signal as financial advice."
)

with st.sidebar:
    st.header("Settings")
    symbol_labels = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X"}
    symbol = st.selectbox("Forex pair", list(symbol_labels.keys()), index=0)
    timeframe_options = {"15m": ("15m", "5d"), "1h": ("60m", "1mo"), "1d": ("1d", "6mo")}
    timeframe = st.selectbox("Chart timeframe", list(timeframe_options.keys()), index=0)
    st.caption("Source: Yahoo Finance via yfinance. Quotes may be delayed; this is not guaranteed real-time data.")
    if st.button("Refresh market data"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.write("**Virtual starting balance**")
    balance_row = conn.execute("SELECT value FROM settings WHERE key='virtual_balance'").fetchone()
    balance = st.number_input("Balance ($)", min_value=100.0, max_value=10000000.0, value=float(balance_row[0]), step=500.0)
    if st.button("Save virtual balance"):
        conn.execute("UPDATE settings SET value=? WHERE key='virtual_balance'", (str(balance),))
        conn.commit()
        st.success("Virtual balance saved.")

tab_dashboard, tab_strategies, tab_paper, tab_notes = st.tabs(
    ["📊 Analyzer", "📚 Strategy Library", "🧪 Paper Trading", "🛠️ Setup & Limits"]
)

with tab_dashboard:
    st.subheader("Forex market analyzer")
    yf_symbol = symbol_labels[symbol]
    interval, period = timeframe_options[timeframe]
    with st.spinner(f"Fetching {symbol} candles..."):
        try:
            data = load_forex_data(yf_symbol, interval, period)
        except Exception as exc:
            data = pd.DataFrame()
            st.error(f"Market data request failed: {exc}")

    if data.empty:
        st.error(
            "Forex candles are not available right now. This may be a provider/network limitation. "
            "No synthetic prices are substituted, so no signal is shown."
        )
        st.info("Try Refresh market data, choose another timeframe, or try again later. Yahoo Finance availability varies by pair and interval.")
    else:
        last = data.iloc[-1]
        prior = data.iloc[-2] if len(data) > 1 else last
        data["sma20"] = data["close"].rolling(20).mean()
        avg = data["sma20"].iloc[-1]
        if pd.notna(avg) and last["close"] > avg and last["close"] > prior["close"]:
            signal, reason = "BUY (TEST RULE)", "Latest close is above its 20-candle average and above the previous close."
        elif pd.notna(avg) and last["close"] < avg and last["close"] < prior["close"]:
            signal, reason = "SELL (TEST RULE)", "Latest close is below its 20-candle average and below the previous close."
        else:
            signal, reason = "WAIT", "The simple test conditions do not agree, or there are not enough candles."
        c1, c2, c3 = st.columns(3)
        c1.metric("Forex pair", symbol)
        c2.metric("Latest close", f"{last['close']:.5f}")
        c3.metric("Educational test signal", signal)
        st.caption(f"Latest candle timestamp reported by provider: {last['time']}")
        st.info(
            f"**Why:** {reason}  \n"
            "**Data status:** downloaded from Yahoo Finance; may be delayed or incomplete. "
            "This is not a validated trading strategy."
        )
        fig = go.Figure(data=[go.Candlestick(
            x=data["time"], open=data["open"], high=data["high"],
            low=data["low"], close=data["close"], name=f"{symbol} candles"
        )])
        fig.add_trace(go.Scatter(x=data["time"], y=data["sma20"], mode="lines", name="20-candle average"))
        fig.update_layout(
            title=f"{symbol} — Yahoo Finance candles (availability/delay may vary)",
            xaxis_title="Time", yaxis_title="Price", height=470, xaxis_rangeslider_visible=False
        )
        st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "The signal is only a simple moving-average demonstration. The eight saved strategies "
        "are not yet combined, validated, or backtested. No live or demo orders are sent."
    )
    st.subheader("Prototype risk calculator")
    col1, col2, col3 = st.columns(3)
    entry = col1.number_input("Example entry", min_value=0.00001, value=1.08000, format="%.5f")
    stop = col2.number_input("Example stop-loss", min_value=0.00000, value=1.07800, format="%.5f")
    rr = col3.selectbox("Reward/risk target", [1.0, 1.5, 2.0, 3.0], index=1)
    risk_distance = abs(entry - stop)
    target = entry + risk_distance * rr if entry >= stop else entry - risk_distance * rr
    st.write(f"Illustrative target based on chosen reward/risk: **{target:.5f}**. This example is not linked to the selected pair and does not calculate account-currency P/L.")

with tab_strategies:
    st.subheader("Strategy database (draft rules)")
    try:
        strategies = load_strategies()
        for item in strategies:
            with st.expander(f"{item['id']} — {item['name']}"):
                st.write(f"**Status:** {item['status']}")
                st.write("**Rules:**")
                st.json(item["rules"])
                st.write("**Risk framework:**")
                st.json(item["risk"])
                st.write("**Still needs defining:**")
                st.write(", ".join(item["unknowns"]))
                st.caption(item["notes"])
        st.download_button("Download strategies.json", data=STRATEGIES_PATH.read_bytes(), file_name="strategies.json", mime="application/json")
    except Exception as exc:
        st.error(f"Could not load strategies.json: {exc}")

with tab_paper:
    st.subheader("Paper-trading journal")
    st.caption("Manual virtual trades only. No orders are sent anywhere.")
    with st.form("new_trade"):
        a, b, c = st.columns(3)
        trade_symbol = a.selectbox("Symbol", ["EURUSD", "GBPUSD", "USDJPY"], key="trade_symbol")
        side = b.selectbox("Side", ["BUY", "SELL"])
        qty = c.number_input("Units (virtual)", min_value=1.0, value=1000.0, step=1000.0)
        a, b, c = st.columns(3)
        trade_entry = a.number_input("Entry price", min_value=0.00001, value=1.08000, format="%.5f", key="trade_entry")
        trade_stop = b.number_input("Stop price", min_value=0.00000, value=1.07800, format="%.5f", key="trade_stop")
        trade_target = c.number_input("Target price", min_value=0.00001, value=1.08400, format="%.5f", key="trade_target")
        submitted = st.form_submit_button("Record virtual trade")
    if submitted:
        valid = (side == "BUY" and trade_stop < trade_entry < trade_target) or (side == "SELL" and trade_target < trade_entry < trade_stop)
        if not valid:
            st.error("Check levels: BUY needs Stop < Entry < Target; SELL needs Target < Entry < Stop.")
        else:
            conn.execute(
                """INSERT INTO paper_trades(created_at,symbol,side,entry,stop,target,quantity,status)
                   VALUES(?,?,?,?,?,?,?,'OPEN')""",
                (datetime.now(timezone.utc).isoformat(), trade_symbol, side, trade_entry, trade_stop, trade_target, qty),
            )
            conn.commit()
            st.success("Virtual trade recorded in the app database. On hosted deployments, local storage may reset.")
            st.rerun()
    trades = load_trades(conn)
    if not trades.empty:
        st.dataframe(trades, use_container_width=True, hide_index=True)
        open_trades = trades[trades.status == "OPEN"]
        if not open_trades.empty:
            ids = open_trades["id"].tolist()
            chosen_id = st.selectbox("Select open trade to close", ids)
            exit_price = st.number_input("Virtual exit price", min_value=0.00001, value=1.08000, format="%.5f")
            if st.button("Close selected virtual trade"):
                row = open_trades[open_trades.id == chosen_id].iloc[0]
                pnl = (exit_price - row.entry) * row.quantity if row.side == "BUY" else (row.entry - exit_price) * row.quantity
                conn.execute("UPDATE paper_trades SET status='CLOSED', exit_price=?, pnl=? WHERE id=?", (exit_price, pnl, int(chosen_id)))
                conn.commit()
                st.success(f"Closed virtual trade. Simplified P/L: {pnl:.2f} price-units × units. This is NOT account-currency P/L; pip value, spread, leverage and conversion are not modeled.")
                st.rerun()
        closed = trades[trades.status == "CLOSED"]
        if not closed.empty:
            wins = int((closed["pnl"] > 0).sum())
            st.metric("Closed-trade win rate", f"{wins / len(closed) * 100:.1f}%", help="Based only on manually entered virtual trades; not evidence of strategy performance.")
    else:
        st.write("No paper trades yet. Add a virtual trade above to begin the journal.")

with tab_notes:
    st.subheader("What is implemented now?")
    st.markdown("""
- Forex candles are requested from Yahoo Finance using `yfinance`.
- Provider data can be delayed, incomplete, or unavailable; this is not guaranteed real-time data.
- If no candles are returned, the app shows an error and does not invent replacement prices.
- Strategy records are stored in `strategies.json`.
- Paper-trade entries and exits use SQLite in the app's local filesystem; hosted persistence is not guaranteed.
- The test signal uses only a simple 20-candle average rule.
- The eight strategy records are drafts, not a validated combined signal engine.
- No broker credentials, live order execution, or automated real-money trading are included.
""")
    st.subheader("Next steps")
    st.markdown("""
1. Confirm the data provider returns candles for the selected pair and timeframe.
2. Define measurable rules for each draft strategy and add tests.
3. Backtest chronologically with realistic spread/fees and avoid look-ahead bias.
4. Paper-test and review drawdown and performance before any broker demo integration.
""")
