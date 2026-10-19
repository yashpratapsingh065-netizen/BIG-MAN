import json
from pathlib import Path
from datetime import datetime, timezone
import sqlite3

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

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

def load_trades(conn):
    return pd.read_sql_query("SELECT * FROM paper_trades ORDER BY id DESC", conn)

def candle_demo():
    # Synthetic data is clearly labelled; it is not live market data.
    idx = pd.date_range(end=pd.Timestamp.now().floor("15min"), periods=80, freq="15min")
    rows = []
    price = 1.0800
    for i, ts in enumerate(idx):
        drift = 0.00008 if i % 20 < 11 else -0.00004
        o = price
        c = o + drift + ((i % 5) - 2) * 0.000025
        h = max(o, c) + 0.00012
        l = min(o, c) - 0.00010
        rows.append((ts, o, h, l, c, 100 + (i * 17) % 80))
        price = c
    return pd.DataFrame(rows, columns=["time","open","high","low","close","volume"])

conn = db()
st.title("🤖 BIG MAN — Trading Lab")
st.caption("Version 1.0 • Strategy library + rule-based demo analyzer + paper-trade journal")
st.warning("Educational prototype only. No broker connection, no live orders, and no guaranteed performance. Demo chart data is synthetic until you connect a real data source.")

with st.sidebar:
    st.header("Settings")
    symbol = st.selectbox("Instrument", ["EURUSD", "GBPUSD", "USDJPY"], index=0)
    timeframe = st.selectbox("Chart timeframe", ["15m", "1h", "4h"], index=0)
    st.caption("Instrument selection is a label only in this prototype; price data below is synthetic.")
    st.divider()
    st.write("**Virtual starting balance**")
    balance_row = conn.execute("SELECT value FROM settings WHERE key='virtual_balance'").fetchone()
    balance = st.number_input("Balance ($)", min_value=100.0, max_value=10000000.0, value=float(balance_row[0]), step=500.0)
    if st.button("Save virtual balance"):
        conn.execute("UPDATE settings SET value=? WHERE key='virtual_balance'", (str(balance),))
        conn.commit()
        st.success("Virtual balance saved.")

tab_dashboard, tab_strategies, tab_paper, tab_notes = st.tabs(["📊 Analyzer", "📚 Strategy Library", "🧪 Paper Trading", "🛠️ Setup & Limits"])

with tab_dashboard:
    st.subheader("Market analyzer")
    data = candle_demo()
    last = data.iloc[-1]
    prior = data.iloc[-2]
    avg = data["close"].rolling(20).mean().iloc[-1]
    # Conservative starter logic: one simple illustrative filter only, not the full eight-strategy engine.
    if last["close"] > avg and last["close"] > prior["close"]:
        signal, reason = "BUY (TEST RULE)", "Synthetic close is above its 20-bar average and the previous close."
    elif last["close"] < avg and last["close"] < prior["close"]:
        signal, reason = "SELL (TEST RULE)", "Synthetic close is below its 20-bar average and the previous close."
    else:
        signal, reason = "WAIT", "The simple test conditions do not agree."
    c1, c2, c3 = st.columns(3)
    c1.metric("Instrument label", symbol)
    c2.metric("Last synthetic close", f"{last['close']:.5f}")
    c3.metric("Test signal", signal)
    st.info(f"**Why:** {reason}  \n**Data status:** synthetic sample data, not a live quote. Do not trade from this signal.")
    fig = go.Figure(data=[go.Candlestick(x=data["time"], open=data["open"], high=data["high"], low=data["low"], close=data["close"], name="Synthetic candles")])
    fig.add_trace(go.Scatter(x=data["time"], y=data["close"].rolling(20).mean(), mode="lines", name="20-bar average"))
    fig.update_layout(title="Illustrative chart — synthetic data only", xaxis_title="Time", yaxis_title="Price", height=470, xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("This first analyzer is a software smoke-test using synthetic candles and a basic moving-average rule. It does not yet implement the eight strategies or use live market data.")
    st.subheader("Prototype risk calculator")
    col1, col2, col3 = st.columns(3)
    entry = col1.number_input("Example entry", min_value=0.00001, value=1.08000, format="%.5f")
    stop = col2.number_input("Example stop-loss", min_value=0.00000, value=1.07800, format="%.5f")
    rr = col3.selectbox("Reward/risk target", [1.0, 1.5, 2.0, 3.0], index=1)
    risk_distance = abs(entry-stop)
    target = entry + risk_distance*rr if entry >= stop else entry - risk_distance*rr
    st.write(f"Illustrative target based on chosen reward/risk: **{target:.5f}**. Set the correct side and instrument-specific pip/tick value before using this for any real decision.")

with tab_strategies:
    st.subheader("Strategy database (draft rules)")
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

with tab_paper:
    st.subheader("Paper-trading journal")
    st.caption("Manual virtual trades only in this build. No orders are sent anywhere.")
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
            conn.execute("""INSERT INTO paper_trades(created_at,symbol,side,entry,stop,target,quantity,status)
                         VALUES(?,?,?,?,?,?,?,'OPEN')""",
                         (datetime.now(timezone.utc).isoformat(), trade_symbol, side, trade_entry, trade_stop, trade_target, qty))
            conn.commit()
            st.success("Virtual trade recorded in local SQLite database.")
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
                pnl = (exit_price-row.entry)*row.quantity if row.side == "BUY" else (row.entry-exit_price)*row.quantity
                conn.execute("UPDATE paper_trades SET status='CLOSED', exit_price=?, pnl=? WHERE id=?", (exit_price, pnl, int(chosen_id)))
                conn.commit()
                st.success(f"Closed virtual trade. Simplified P/L: {pnl:.2f} price-units × units. This is NOT account-currency P/L; pip value, leverage, spread and conversion are not modeled.")
                st.rerun()
        closed = trades[trades.status == "CLOSED"]
        if not closed.empty:
            wins = int((closed["pnl"] > 0).sum())
            st.metric("Closed-trade win rate", f"{wins/len(closed)*100:.1f}%", help="Based only on manually entered virtual trades; not evidence of strategy performance.")
    else:
        st.write("No paper trades yet. Add a virtual trade above to begin the journal.")

with tab_notes:
    st.subheader("What is implemented now?")
    st.markdown("""
- Strategy records are stored in `strategies.json`.
- Paper-trade entries and exits are stored in local `big_man.db` (SQLite).
- Analyzer chart uses **synthetic sample data**, not live prices.
- The BUY/SELL test signal uses a simple moving-average illustration only.
- All eight strategy records are drafts and are **not yet combined into a validated signal engine**.
- No broker credentials, API keys, live order execution, leverage, or automated real-money trading are included.
""")
    st.subheader("Next steps")
    st.markdown("""
1. Run the app and confirm the pages work.
2. Choose a reliable Forex market-data provider and connect historical candles.
3. Define every strategy as measurable rules and add tests.
4. Backtest chronologically with spread/fees and avoid look-ahead bias.
5. Run demo/paper tests and review drawdown and performance before considering any broker demo integration.
""")
