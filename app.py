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

st.set_page_config(page_title="BIG MAN Trading Lab", page_icon="🤖", layout="wide")


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
        side TEXT NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
        quantity REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
        exit_price REAL, pnl REAL, exit_reason TEXT, strategy_note TEXT
    )""")
    # Safely add fields if an older database already exists.
    existing = {r[1] for r in con.execute("PRAGMA table_info(paper_trades)").fetchall()}
    for name, typ in [("timeframe", "TEXT"), ("exit_reason", "TEXT"), ("strategy_note", "TEXT")]:
        if name not in existing:
            con.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {typ}")
    con.execute("""CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('virtual_balance','10000')")
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('auto_paper','0')")
    con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('last_processed_candle','')")
    con.commit()
    return con


def get_setting(con, key, default=""):
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(con, key, value):
    con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    con.commit()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles(ticker, interval, period):
    frame = yf.download(ticker, period=period, interval=interval, auto_adjust=False, progress=False, threads=False)
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    time_col = "Datetime" if "Datetime" in frame.columns else "Date"
    if time_col not in frame.columns:
        time_col = frame.columns[0]
    needed = ["Open", "High", "Low", "Close"]
    if not all(c in frame.columns for c in needed):
        return pd.DataFrame()
    out = pd.DataFrame({
        "time": pd.to_datetime(frame[time_col], errors="coerce", utc=True),
        "open": pd.to_numeric(frame["Open"], errors="coerce"),
        "high": pd.to_numeric(frame["High"], errors="coerce"),
        "low": pd.to_numeric(frame["Low"], errors="coerce"),
        "close": pd.to_numeric(frame["Close"], errors="coerce"),
    })
    return out.dropna().tail(500).reset_index(drop=True)


def strategy_signals(data):
    """Transparent rule-based proxies inspired by the library themes, not validated versions of the original strategies."""
    d = data.copy()
    d["sma20"] = d.close.rolling(20).mean()
    d["sma50"] = d.close.rolling(50).mean()
    d["prior_high"] = d.high.shift(1).rolling(20).max()
    d["prior_low"] = d.low.shift(1).rolling(20).min()
    d["range"] = d.high - d.low
    d["atr14"] = d["range"].rolling(14).mean()
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last
    close, opn, high, low = [float(last[k]) for k in ("close", "open", "high", "low")]
    signals = []
    def add(name, side, why):
        signals.append({"Strategy": name, "Signal": side, "Reason": why})

    # S/R proxy: close near prior 20-candle extremes with candle confirmation.
    pl, ph = last.get("prior_low"), last.get("prior_high")
    if pd.notna(pl) and low <= float(pl) * 1.001 and close > opn:
        add("SR_001 Support & Resistance", "BUY", "Near recent low with bullish candle (proxy rule).")
    elif pd.notna(ph) and high >= float(ph) * 0.999 and close < opn:
        add("SR_001 Support & Resistance", "SELL", "Near recent high with bearish candle (proxy rule).")
    else: add("SR_001 Support & Resistance", "WAIT", "No clear recent-extreme confirmation.")

    # Fibonacci proxy uses recent 50-candle swing and 38.2%-61.8% retracement band.
    recent = d.tail(50)
    swing_low, swing_high = float(recent.low.min()), float(recent.high.max())
    band_low = swing_high - (swing_high - swing_low) * 0.618
    band_high = swing_high - (swing_high - swing_low) * 0.382
    if band_low <= close <= band_high and close > opn: add("FIB_001 Fibonacci + Price Action", "BUY", "In approximate retracement band with bullish candle.")
    elif band_low <= close <= band_high and close < opn: add("FIB_001 Fibonacci + Price Action", "SELL", "In approximate retracement band with bearish candle.")
    else: add("FIB_001 Fibonacci + Price Action", "WAIT", "No retracement-band candle confirmation.")

    if pd.notna(ph) and close > float(ph): add("CR_001 Chart Reading / Breakout", "BUY", "Close above prior 20-candle high.")
    elif pd.notna(pl) and close < float(pl): add("CR_001 Chart Reading / Breakout", "SELL", "Close below prior 20-candle low.")
    else: add("CR_001 Chart Reading / Breakout", "WAIT", "No 20-candle breakout.")

    # Fake-breakout proxy: price wicks beyond previous range but closes back inside.
    if pd.notna(ph) and high > float(ph) and close < float(ph): add("TRAP_001 Fake Breakout", "SELL", "Wick above recent high then close back below.")
    elif pd.notna(pl) and low < float(pl) and close > float(pl): add("TRAP_001 Fake Breakout", "BUY", "Wick below recent low then close back above.")
    else: add("TRAP_001 Fake Breakout", "WAIT", "No clear failed breakout.")

    if pd.notna(last.get("sma20")) and pd.notna(last.get("sma50")):
        if close > float(last.sma20) > float(last.sma50): add("MTF_001 Trend Alignment (single-TF proxy)", "BUY", "Close above SMA20 above SMA50; true multi-timeframe check not implemented.")
        elif close < float(last.sma20) < float(last.sma50): add("MTF_001 Trend Alignment (single-TF proxy)", "SELL", "Close below SMA20 below SMA50; true multi-timeframe check not implemented.")
        else: add("MTF_001 Trend Alignment (single-TF proxy)", "WAIT", "Moving averages not aligned.")
    else: add("MTF_001 Trend Alignment (single-TF proxy)", "WAIT", "Need more candles.")

    body = close - opn
    prev_body = float(prev.close) - float(prev.open)
    if body > 0 and prev_body > 0 and close > float(prev.close): add("PA_001 Three-Step Price Action", "BUY", "Two bullish candles with rising close (proxy rule).")
    elif body < 0 and prev_body < 0 and close < float(prev.close): add("PA_001 Three-Step Price Action", "SELL", "Two bearish candles with falling close (proxy rule).")
    else: add("PA_001 Three-Step Price Action", "WAIT", "No two-candle momentum confirmation.")

    # Trendline proxy approximated by recent swing range, not a hand-drawn trendline.
    if pd.notna(last.get("sma20")) and close > float(last.sma20) and low <= float(last.sma20) and close > opn: add("TL_001 Trendline Bounce (SMA proxy)", "BUY", "Bullish candle touching SMA20 proxy.")
    elif pd.notna(last.get("sma20")) and close < float(last.sma20) and high >= float(last.sma20) and close < opn: add("TL_001 Trendline Bounce (SMA proxy)", "SELL", "Bearish candle touching SMA20 proxy.")
    else: add("TL_001 Trendline Bounce (SMA proxy)", "WAIT", "No SMA-touch bounce proxy.")

    # Supply/demand proxy from prior range extremes.
    if pd.notna(pl) and low <= float(pl) * 1.002 and close > opn: add("SD_001 Supply & Demand (range proxy)", "BUY", "Bullish reaction near recent range low.")
    elif pd.notna(ph) and high >= float(ph) * 0.998 and close < opn: add("SD_001 Supply & Demand (range proxy)", "SELL", "Bearish reaction near recent range high.")
    else: add("SD_001 Supply & Demand (range proxy)", "WAIT", "No reaction near recent range edge.")
    return pd.DataFrame(signals), last


def close_open_trades_on_candle(con, symbol, last):
    rows = con.execute("SELECT id,side,entry,stop,target,quantity FROM paper_trades WHERE status='OPEN' AND symbol=?", (symbol,)).fetchall()
    close_events = []
    for trade_id, side, entry, stop, target, qty in rows:
        hit = None
        exit_price = None
        # Conservative assumption: if SL and TP are both inside same candle, assume SL first.
        if side == "BUY":
            if float(last.low) <= stop: hit, exit_price = "STOP_LOSS", stop
            elif float(last.high) >= target: hit, exit_price = "TAKE_PROFIT", target
        else:
            if float(last.high) >= stop: hit, exit_price = "STOP_LOSS", stop
            elif float(last.low) <= target: hit, exit_price = "TAKE_PROFIT", target
        if hit:
            pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
            con.execute("UPDATE paper_trades SET status='CLOSED',exit_price=?,pnl=?,exit_reason=? WHERE id=?", (exit_price, pnl, hit, trade_id))
            close_events.append(f"Paper trade #{trade_id} closed at {exit_price:.5f}: {hit}; simplified P/L ${pnl:.2f}.")
    con.commit()
    return close_events


def create_auto_trade(con, symbol, timeframe, side, last, signals, balance):
    if con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0] > 0:
        return "No new trade: one open paper trade already exists."
    atr = last.get("atr14")
    if pd.isna(atr) or float(atr) <= 0:
        return "No trade: not enough candles to calculate the stop distance."
    entry = float(last.close)
    stop_distance = max(float(atr) * 1.5, entry * 0.001)
    stop = entry - stop_distance if side == "BUY" else entry + stop_distance
    target = entry + stop_distance * 1.5 if side == "BUY" else entry - stop_distance * 1.5
    # EURUSD's price difference × EUR units approximates USD P/L; this is a simplified demo calculation.
    risk_cash = max(1.0, float(balance) * 0.005)  # 0.5% virtual-balance risk budget
    qty = risk_cash / stop_distance
    qty = min(qty, 10000.0)  # cap virtual position size for the prototype
    note = f"Auto paper rule: at least 5 of 8 proxy signals agree {side}; risk budget approx 0.5% virtual balance."
    con.execute("""INSERT INTO paper_trades(created_at,symbol,timeframe,side,entry,stop,target,quantity,status,strategy_note)
        VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?)""", (datetime.now(timezone.utc).isoformat(), symbol, timeframe, side, entry, stop, target, qty, note))
    con.commit()
    return f"Opened AUTO PAPER {side} at {entry:.5f}; SL {stop:.5f}; TP {target:.5f}; virtual units {qty:.0f}."


con = db()
st.title("🤖 BIG MAN — Trading Lab")
st.caption("Version 2.0 • EUR/USD H1-first multi-strategy prototype + automatic paper trading")
st.warning("EDUCATIONAL PAPER-TRADING ONLY. No broker orders are sent. Yahoo Finance data can be delayed or incomplete. This is not financial advice and the strategy proxies are not validated.")

with st.sidebar:
    st.header("Settings")
    pair_options = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X"}
    symbol = st.selectbox("Forex pair", list(pair_options.keys()), index=0)
    tf_options = {"H1 (1 hour)": ("60m", "1mo"), "M15 (15 minutes)": ("15m", "5d"), "D1 (1 day)": ("1d", "6mo")}
    timeframe_label = st.selectbox("Chart timeframe", list(tf_options.keys()), index=0)
    interval, period = tf_options[timeframe_label]
    balance_saved = float(get_setting(con, "virtual_balance", "10000"))
    balance = st.number_input("Virtual balance ($)", min_value=100.0, max_value=10000000.0, value=balance_saved, step=500.0)
    if st.button("Save virtual balance"):
        set_setting(con, "virtual_balance", balance)
        st.success("Virtual balance saved.")
    auto_on = get_setting(con, "auto_paper", "0") == "1"
    new_auto = st.toggle("Enable automatic PAPER trading", value=auto_on, help="Only simulated trades. Runs when the app reruns/refreshes; it is not a 24/7 background service.")
    if new_auto != auto_on:
        set_setting(con, "auto_paper", "1" if new_auto else "0")
        st.rerun()
    st.caption("Data: Yahoo Finance. Automatic paper rules are evaluated when this page runs; not continuously in the background.")
    if st.button("Refresh market data"):
        st.cache_data.clear()
        st.rerun()

analyzer, strategy_tab, paper_tab, limits_tab = st.tabs(["📊 Analyzer", "📚 Strategy Library", "🧪 Paper Trading", "🛠️ Setup & Limits"])

with analyzer:
    st.subheader(f"{symbol} — {timeframe_label}")
    try:
        data = fetch_candles(pair_options[symbol], interval, period)
    except Exception as exc:
        data = pd.DataFrame()
        st.error(f"Market data fetch failed: {exc}")
    if data.empty or len(data) < 3:
        st.error("Yahoo Finance did not return enough candles. No automatic trade can be evaluated right now.")
    else:
                signal_df, last = strategy_signals(data)
        votes = signal_df[signal_df.Signal.isin(["BUY", "SELL"])].groupby("Signal").size().to_dict()
        buys, sells = int(votes.get("BUY", 0)), int(votes.get("SELL", 0))
        combined = "BUY" if buys >= 5 and buys > sells else "SELL" if sells >= 5 and sells > buys else "WAIT"
        candle_key = f"{symbol}|{interval}|{last.time}"
        open_count = int(
            con.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'"
            ).fetchone()[0]
        )

        auto_enabled = get_setting(con, "auto_paper", "0") == "1"

        already_processed = (
            get_setting(con, "last_processed_candle", "") == candle_key
        )

        st.subheader("🤖 Auto Trading Status")

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "Automatic Paper Trading",
            "ON 🟢" if auto_enabled else "OFF 🔴"
        )

        c2.metric("Open Paper Trades", open_count)

        c3.metric(
            "Candle Checked",
            "YES" if already_processed else "NO"
        )

        if not auto_enabled:
            st.warning(
                "Auto Trading OFF hai. Sidebar mein Enable automatic PAPER trading ON karo."
            )

        elif combined == "WAIT":
            st.info(
                f"Abhi trade nahi khula. BUY votes: {buys}, "
                f"SELL votes: {sells}. "
                "BUY ya SELL ke liye kam se kam 5 agreeing votes chahiye."
            )

        elif open_count > 0:
            st.info(
                "Ek paper trade pehle se OPEN hai. "
                "Naya trade tabhi khulega jab purana trade close ho."
            )

        elif already_processed:
            st.info(
                "Is candle ki checking pehle ho chuki hai. "
                "Nayi candle aane par dobara check hoga."
            )

        else:
            st.success(
                f"{combined} signal mila hai. "
                "Paper trade ki entry conditions check hongi."
            )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Latest close", f"{float(last.close):.5f}")
        c2.metric("BUY votes", buys)
        c3.metric("SELL votes", sells)
        c4.metric("Combined signal", combined)
        st.caption(f"Latest provider candle timestamp (UTC): {last.time}. Last candle may still be forming.")
        st.dataframe(signal_df, use_container_width=True, hide_index=True)
        fig = go.Figure(data=[go.Candlestick(x=data.time, open=data.open, high=data.high, low=data.low, close=data.close, name=symbol)])
        data["sma20"] = data.close.rolling(20).mean()
        fig.add_trace(go.Scatter(x=data.time, y=data.sma20, mode="lines", name="SMA 20"))
        fig.update_layout(title=f"{symbol} — Yahoo Finance candles", xaxis_title="Time", yaxis_title="Price", height=430, xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)
        st.info("These eight are simplified proxy rules inspired by the saved strategy themes—not faithful, backtested or proven versions. MTF, Fibonacci, trendline and zones are approximations.")

        # Evaluate exits on latest candle, once per new provider candle. On refresh/rerun, no repeated entry on same candle.
        candle_key = f"{symbol}|{interval}|{last.time}"
        close_messages = close_open_trades_on_candle(con, symbol, last)
        for msg in close_messages: st.success(msg)
        already_processed = get_setting(con, "last_processed_candle", "") == candle_key
        if get_setting(con, "auto_paper", "0") == "1" and not already_processed:
            if combined in ("BUY", "SELL"):
                msg = create_auto_trade(con, symbol, timeframe_label, combined, last, signal_df, balance)
                st.success(msg) if msg.startswith("Opened") else st.info(msg)
            else:
                st.info("Auto paper mode is ON, but combined signal is WAIT; no trade opened.")
            set_setting(con, "last_processed_candle", candle_key)
        elif get_setting(con, "auto_paper", "0") == "1" and already_processed:
            st.caption("This candle was already evaluated; refresh after a new candle is available to evaluate again.")
        st.caption("Auto trading checks candles only when this app runs or refreshes. It is NOT a continuously running 24/7 bot. If both SL and TP fall within one candle, this prototype assumes SL first.")

with strategy_tab:
    st.subheader("Saved strategy library")
    try:
        strategy_items = json.loads(STRATEGIES_PATH.read_text(encoding="utf-8"))
        for item in strategy_items:
            with st.expander(f"{item.get('id','Strategy')} — {item.get('name','Unnamed')}"):
                st.write(f"**Status:** {item.get('status','Draft')}")
                st.json(item.get("rules", {}))
                st.write("**Risk:**")
                st.json(item.get("risk", {}))
                st.write("**Unknowns:**", ", ".join(item.get("unknowns", [])))
                st.caption(item.get("notes", ""))
        st.download_button("Download strategies.json", data=STRATEGIES_PATH.read_bytes(), file_name="strategies.json", mime="application/json")
    except Exception as exc:
        st.error(f"Could not load strategies.json: {exc}")

with paper_tab:
    st.subheader("Automatic paper-trade journal")
    st.caption("Max one open paper trade across all pairs. P/L is simplified and excludes spread, slippage, swap and conversion details.")
    trades = pd.read_sql_query("SELECT * FROM paper_trades ORDER BY id DESC", con)
    if trades.empty:
        st.write("No paper trades yet. Enable automatic PAPER trading in the sidebar; an entry only happens if the combined proxy signal qualifies.")
    else:
        st.dataframe(trades, use_container_width=True, hide_index=True)
        open_rows = trades[trades.status == "OPEN"]
        if not open_rows.empty:
            st.metric("Open paper trade", f"#{int(open_rows.iloc[0]['id'])} {open_rows.iloc[0]['side']} {open_rows.iloc[0]['symbol']}")
        closed = trades[trades.status == "CLOSED"]
        if not closed.empty:
            wins = int((closed.pnl > 0).sum())
            col1, col2, col3 = st.columns(3)
            col1.metric("Closed trades", len(closed))
            col2.metric("Win rate", f"{wins / len(closed) * 100:.1f}%")
            col3.metric("Simplified net P/L", f"${closed.pnl.sum():.2f}")
    st.warning("This is a software prototype, not a reliable trading system. Do not use these signals for real-money decisions.")

with limits_tab:
    st.subheader("What this version does and does not do")
    st.markdown("""
- Fetches Yahoo Finance candles when the app runs or is refreshed.
- Shows eight separate **proxy** signals and a combined signal (5 agreeing votes required).
- Can create simulated paper trades if automatic PAPER mode is enabled.
- Allows at most one open paper trade across all pairs.
- Uses an ATR-like recent candle-range stop and a 1.5R target, with a 0.5% virtual-balance risk budget (position size capped).
- Checks stop/target against the latest candle only when the app runs; it is not a background 24/7 service.
- No broker connection, no demo-account orders, no real-money orders.
- Strategies are approximations, not validated implementations. No backtest or profitability claim has been made.
- Paper-trade database may reset on some hosted deployments; do not treat it as permanent storage.
    """)
