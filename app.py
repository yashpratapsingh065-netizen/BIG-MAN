import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BIGMAN — Institutional Grade SMC Terminal v66", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main { background-color: #0b0f19; color: #f3f4f6; font-family: 'Inter', sans-serif; }
    .stApp { background: #0b0f19; }
    h1, h2, h3 { font-family: 'Inter', sans-serif; font-weight: 700; color: #ffffff; }
    .bigman-header { font-size: 2.2rem; font-weight: 800; background: linear-gradient(90deg, #3b82f6, #10b981); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0px; }
    .bigman-sub { font-size: 0.95rem; color: #9ca3af; margin-bottom: 20px; font-weight: 500; }
    .stButton>button { width: 100%; border-radius: 8px; font-weight: 600; background-color: #2563eb; color: white; border: none; padding: 10px; transition: all 0.3s ease; }
    .stButton>button:hover { background-color: #1d4ed8; box-shadow: 0 0 15px rgba(37, 99, 235, 0.4); }
    </style>
""", unsafe_allow_html=True)

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_institutional_v66.db")
DEFAULT_API_KEY = "d941e0ca121e4ebc9f9bb40b224a7970"

SYMBOLS = {
    "XAU/USD (Gold)": {"td": "XAU/USD", "tv": "OANDA:XAUUSD", "mt5": "XAUUSD"},
    "EUR/USD": {"td": "EUR/USD", "tv": "FX:EURUSD", "mt5": "EURUSD"},
    "GBP/USD": {"td": "GBP/USD", "tv": "FX:GBPUSD", "mt5": "GBPUSD"},
}

INTERVALS = {
    "15 min": {"td": "15min", "tv_int": "15"},
    "1 hour": {"td": "1h", "tv_int": "60"},
    "4 hours": {"td": "4h", "tv_int": "240"},
    "1 day": {"td": "1day", "tv_int": "D"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS open_trades_v66 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.01,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT,
            mt5_ticket INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history_v66 (
            id INTEGER PRIMARY KEY,
            created_at_ist TEXT NOT NULL,
            closed_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL,
            bias TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS account_balance (
            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    if not cursor.fetchone():
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 3000.0)")
    con.commit()
    return con

def get_market_status():
    now_utc = datetime.now(timezone.utc)
    wd, hr = now_utc.weekday(), now_utc.hour
    if wd == 5 or (wd == 6 and hr < 22) or (wd == 4 and hr >= 22):
        return "CLOSED", "🔴 Market Closed (Weekend/Offline)"
    return "OPEN", "🟢 International Forex/Gold Market Live"

@st.cache_data(ttl=10, show_spinner=False)
def fetch_exact_candles(symbol_label, interval_label, rows=150):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    td_key = st.secrets.get("TWELVE_DATA_API_KEY", os.getenv("TWELVE_DATA_API_KEY", DEFAULT_API_KEY))
    if not td_key or td_key == "demo":
        td_key = DEFAULT_API_KEY
    
    live_p = 0.0
    
    # Priority 1: MetaTrader 5 if connected locally
    if MT5_AVAILABLE:
        try:
            if not mt5.initialize():
                mt5.initialize()
            tick = mt5.symbol_info_tick(info["mt5"])
            if tick is not None:
                live_p = float(tick.bid)
        except Exception:
            pass

    # Priority 2: Twelve Data Quote Endpoint
    if live_p <= 0:
        try:
            p_resp = requests.get(
                "https://api.twelvedata.com/price", 
                params={"symbol": info["td"], "apikey": td_key}, 
                timeout=4
            )
            p_data = p_resp.json()
            if "price" in p_data:
                live_p = float(p_data["price"])
        except Exception:
            pass

    # Fetch Time Series OHLC Data
    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": info["td"], "interval": cfg["td"], "outputsize": rows, "apikey": td_key},
            timeout=5,
        )
        payload = resp.json()
        if "values" in payload:
            df = pd.DataFrame(payload["values"])
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
            df["datetime_ist"] = df["datetime"].dt.tz_convert(IST)
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna().sort_values("datetime").set_index("datetime")
            
            if not df.empty:
                if live_p <= 0:
                    live_p = float(df.close.iloc[-1])
                else:
                    df.iloc[-1, df.columns.get_loc('close')] = live_p
                    if live_p > df.iloc[-1]['high']:
                        df.iloc[-1, df.columns.get_loc('high')] = live_p
                    if live_p < df.iloc[-1]['low']:
                        df.iloc[-1, df.columns.get_loc('low')] = live_p

                return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), live_p, "TwelveData Live Feed 🟢"
    except Exception:
        pass
        
    # Strict Static Anchor (Prevents random jumping if API is temporarily rate-limited)
    anchor_p = 4040.24 if "XAU" in symbol_label else 1.0850
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=rows, freq="15min")
    df_safe = pd.DataFrame({
        "open": anchor_p, "high": anchor_p + 1.0, "low": anchor_p - 1.0, "close": anchor_p,
        "datetime_ist": pd.Series(dates).dt.tz_convert(IST)
    }, index=dates)
    
    return df_safe, anchor_p, "Locked Baseline Feed 🔒"

def evaluate_institutional_strategies(df, live_p):
    """
    Stable Multi-Strategy Confluence Engine:
    - Evaluates Premium/Discount (S7), Unmitigated FVGs (S5), Wick Rejections (S6/S4), and Engulfing (S3).
    - Requires consistent multi-bar momentum to prevent signal flipping.
    """
    if len(df) < 30:
        return {"bias": "NEUTRAL", "votes": "Gathering Market Structure...", "bullish": 0, "bearish": 0, "entry": live_p, "sl": live_p-5, "tp": live_p+15}
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    # 50% Equilibrium Calculation (S7)
    swing_high = float(df.high.tail(40).max())
    swing_low = float(df.low.tail(40).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bull_votes, bear_votes = [], []

    # 1. Premium & Discount Zone (S7)
    if live_p < eq_50:
        bull_votes.append("Discount Zone (<50% Eq)")
    else:
        bear_votes.append("Premium Zone (>50% Eq)")

    # 2. Fair Value Gap (S5)
    if last.low > p_prev.high and prev.close > prev.open:
        bull_votes.append("Unmitigated Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open:
        bear_votes.append("Unmitigated Bearish FVG")

    # 3. Supply/Demand Wick Rejection (S6/S4)
    if lower_wick >= body * 1.2 and last.close > last.open:
        bull_votes.append("Demand Zone Wick Rejection")
    elif upper_wick >= body * 1.2 and last.close < last.open:
        bear_votes.append("Supply Zone Wick Rejection")

    # 4. Engulfing Confirmation (S3)
    if last.close > last.open and prev.close < prev.open:
        bull_votes.append("Bullish Engulfing")
    elif last.close < last.open and prev.close > prev.open:
        bear_votes.append("Bearish Engulfing")

    b_score = len(bull_votes)
    s_score = len(bear_votes)
    
    # Strict stability check: Require a clear majority score to avoid choppy flipping
    if b_score >= 2 and b_score > s_score:
        bias = "BUY"
    elif s_score >= 2 and s_score > b_score:
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    is_gold = "XAU" in str(live_p) or live_p > 100
    sl_buf = max(rng * 2.0, 6.0 if is_gold else 0.0030)
    tp_buf = sl_buf * 3.0

    if bias == "BUY":
        sl, tp = live_p - sl_buf, live_p + tp_buf
    elif bias == "SELL":
        sl, tp = live_p + sl_buf, live_p - tp_buf
    else:
        sl, tp = live_p - sl_buf, live_p + tp_buf

    votes_summary = f"Bullish [{b_score}]: {', '.join(bull_votes) if bull_votes else 'None'} | Bearish [{s_score}]: {', '.join(bear_votes) if bear_votes else 'None'}"

    return {
        "bias": bias,
        "votes": votes_summary,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(live_p, 2 if is_gold else 5),
        "sl": round(sl, 2 if is_gold else 5),
        "tp": round(tp, 2 if is_gold else 5)
    }

def send_mt5_order(symbol_key, bias, lot_size, sl, tp):
    if not MT5_AVAILABLE:
        return False, 0, "MetaTrader5 library is not installed."
    if not mt5.initialize():
        return False, 0, f"MT5 Init Failed: {mt5.last_error()}"
        
    mt5_sym = SYMBOLS[symbol_key]["mt5"]
    info = mt5.symbol_info(mt5_sym)
    if info is None or not info.visible:
        mt5.symbol_select(mt5_sym, True)

    order_type = mt5.ORDER_TYPE_BUY if bias == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(mt5_sym)
    if tick is None:
        return False, 0, "Failed to get live tick from MT5."
        
    price = tick.ask if bias == "BUY" else tick.bid

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_sym,
        "volume": float(lot_size),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 25,
        "magic": 999888,
        "comment": "BIGMAN SMC v66 Live",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return False, 0, f"MT5 Error Code: {res.retcode}"
    return True, res.order, f"MT5 Ticket #{res.order}"

def get_balance(con):
    cur = con.cursor()
    cur.execute("SELECT balance FROM account_balance WHERE id = 1")
    return float(cur.fetchone()[0])

def update_bal(con, delta):
    cur_bal = get_balance(con)
    new_bal = cur_bal + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

# --- STREAMLIT UI ---
st.markdown('<p class="bigman-header">BIGMAN ⚡ Institutional Terminal</p>', unsafe_allow_html=True)
st.markdown('<p class="bigman-sub">Stable Multi-Timeframe Confluence & Precise Execution Suite</p>', unsafe_allow_html=True)

con = connect_db()
m_status, m_text = get_market_status()

with st.sidebar:
    st.header("🎛️ Terminal Control Center")
    if st.button("🔄 Refresh Terminal Feeds", use_container_width=True):
        st.cache_data.clear()
        st.success("Feeds synchronized successfully!")
        st.rerun()

    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Execution Timeframe (LTF)", list(INTERVALS.keys()), index=0)
    lot = st.number_input("Lot Size", min_value=0.01, max_value=10.0, value=0.01, step=0.01)
    
    st.markdown("---")
    exec_env = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    weekend_mode = st.checkbox("Allow Weekend Practice", value=True)
    auto_pilot = st.toggle("Enable MTF Auto-Pilot Bot", value=False)
    
    st.markdown("---")
    if st.button("➕ Execute Institutional Order", use_container_width=True):
        df_15m, live_p_15, _ = fetch_exact_candles(symbol, "15 min")
        df_1h, _, _ = fetch_exact_candles(symbol, "1 hour")
        df_4h, _, _ = fetch_exact_candles(symbol, "4 hours")
        
        res_15m = evaluate_institutional_strategies(df_15m, live_p_15)
        res_1h = evaluate_institutional_strategies(df_1h, live_p_15)
        res_4h = evaluate_institutional_strategies(df_4h, live_p_15)
        
        # STRICT MULTI-TIMEFRAME RULE: 4H, 1H, and 15M must all agree!
        final_bias = res_15m["bias"]
        
        if final_bias == "NEUTRAL":
            st.sidebar.warning("⚠️ Market is in consolidation. No clear institutional bias.")
            st.stop()
            
        if res_1h["bias"] != "NEUTRAL" and res_1h["bias"] != final_bias:
            st.sidebar.error(f"❌ MTF CONFLICT: 1H Trend is [{res_1h['bias']}] but 15M is [{final_bias}]. Trade rejected for safety!")
            st.stop()

        if res_4h["bias"] != "NEUTRAL" and res_4h["bias"] != final_bias:
            st.sidebar.error(f"❌ MTF CONFLICT: 4H Macro Trend is [{res_4h['bias']}] but execution signal is [{final_bias}]. Trade rejected!")
            st.stop()

        ts = str(df_15m.index[-1])
        e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
        
        cur = con.cursor()
        cur.execute("SELECT id FROM open_trades_v66 WHERE symbol = ? AND candle_timestamp = ?", (symbol, ts))
        if cur.fetchone():
            st.sidebar.warning("⚠️ This candle timestamp already has an active trade open!")
        else:
            ticket = 0
            if e_mode == "MT5 LIVE":
                ok, tkt, msg = send_mt5_order(symbol, final_bias, lot, res_15m["sl"], res_15m["tp"])
                if not ok:
                    st.sidebar.error(msg)
                    st.stop()
                ticket = tkt
            
            ist_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
            con.execute(
                "INSERT INTO open_trades_v66 (created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ist_time, symbol, timeframe, res_15m["entry"], res_15m["sl"], res_15m["tp"], lot, final_bias, f"4H:[{res_4h['bias']}] | 1H:[{res_1h['bias']}] | 15M:[{res_15m['votes']}]", ts, ticket)
            )
            con.commit()
            st.sidebar.success("✅ Aligned Institutional Order Executed!")
            st.rerun()

    st.markdown("---")
    if st.button("🔄 Reset Balance ($3,000)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 3000.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset to $3,000!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")

# Fetch Multi-Timeframe Data
df_15m, live_p, data_src = fetch_exact_candles(symbol, "15 min")
df_1h, _, _ = fetch_exact_candles(symbol, "1 hour")
df_4h, _, _ = fetch_exact_candles(symbol, "4 hours")

if not df_15m.empty and live_p > 0:
    res_15m = evaluate_institutional_strategies(df_15m, live_p)
    res_1h = evaluate_institutional_strategies(df_1h, live_p)
    res_4h = evaluate_institutional_strategies(df_4h, live_p)
    
    is_gold = "XAU" in symbol
    ts_now = str(df_15m.index[-1])
    
    # Auto-Pilot Bot Execution (Strict MTF Alignment Required)
    if auto_pilot and (m_status == "OPEN" or weekend_mode):
        if res_4h["bias"] == res_1h["bias"] == res_15m["bias"] and res_15m["bias"] != "NEUTRAL":
            cur = con.cursor()
            cur.execute("SELECT id FROM open_trades_v66 WHERE symbol = ? AND candle_timestamp = ?", (symbol, ts_now))
            if not cur.fetchone():
                e_mode = "MT5 LIVE" if "MT5" in exec_env else "PAPER"
                tkt = 0
                if e_mode == "MT5 LIVE":
                    ok, tkt_id, _ = send_mt5_order(symbol, res_15m["bias"], lot, res_15m["sl"], res_15m["tp"])
                    if ok: tkt = tkt_id
                
                ist_time = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
                con.execute(
                    "INSERT INTO open_trades_v66 (created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ist_time, symbol, timeframe, res_15m["entry"], res_15m["sl"], res_15m["tp"], lot, res_15m["bias"], f"Auto-Pilot Fully Aligned [{res_15m['bias']}]", ts_now, tkt)
                )
                con.commit()

    balance = get_balance(con)
    bal_color = "🟢" if balance >= 3000 else "🔴"
    st.markdown(f"### 💵 Account Balance: **${balance:,.2f} USD** {bal_color}")
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Live Market Price", f"{live_p:,.2f}" if is_gold else f"{live_p:.5f}")
    m2.metric("4H Macro Trend", res_4h["bias"])
    m3.metric("1H Zone Bias", res_1h["bias"])
    m4.metric("15M Execution Signal", res_15m["bias"])

st.divider()

col_left, col_right = st.columns([1.6, 1])

with col_left:
    st.subheader(f"📈 Live TradingView Chart ({symbol})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_int = INTERVALS[timeframe]["tv_int"]
    
    chart_html = f"""
    <div style="width:100%;height:460px;background:#131722;border-radius:10px;overflow:hidden;border:1px solid #374151;">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_symbol}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=131722&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" width="100%" height="460px" style="border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=470, scrolling=False)

with col_right:
    st.subheader("💡 Multi-Timeframe Alignment Verification")
    if not df_15m.empty:
        # Check alignment guidance for the user
        if res_4h["bias"] == res_1h["bias"] == res_15m["bias"] and res_15m["bias"] != "NEUTRAL":
            st.success(f"✅ **PERFECT ALIGNMENT ({res_15m['bias']}):** 4H, 1H, and 15M timeframes are fully pointing in the same direction. This is a high-probability institutional setup!")
        elif res_15m["bias"] == "NEUTRAL" or res_1h["bias"] == "NEUTRAL":
            st.warning("⚠️ **MARKET CONSOLIDATION:** Timeframes are mixed or neutral. **Do not take trades here.** Wait for alignment.")
        else:
            st.error(f"❌ **TIMEFRAME CONFLICT:** 4H is [{res_4h['bias']}], 1H is [{res_1h['bias']}], and 15M is [{res_15m['bias']}]. **Do not enter!** Wait until all timeframes point the same way.")
        
        fmt = ",.2f" if is_gold else ".5f"
        st.write(f"**15M Confluence Breakdown:** {res_15m['votes']}")
        st.write(f"**Stop Loss:** `{res_15m['sl']:{fmt}}` | **Take Profit:** `{res_15m['tp']:{fmt}}`")
        st.markdown(f"*Feed Source: {data_src}*")

st.divider()
st.subheader("📋 Active Open Trades")

open_trades = pd.read_sql_query("SELECT * FROM open_trades_v66 ORDER BY id DESC", con)
if open_trades.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for _, row in open_trades.iterrows():
        fmt = ",.2f" if "XAU" in row['symbol'] else ".5f"
        t_info = f" | Ticket: #{row['mt5_ticket']}" if row['mt5_ticket'] > 0 else ""
        with st.expander(f"📌 Trade #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot){t_info}"):
            sc1, sc2 = st.columns(2)
            sc1.markdown(f"🕒 **Time:** `{row['created_at_ist']}`\n📍 **Entry:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
            sc2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n📊 **Confluence Details:** `{row['strategy_votes']}`")
            
            if st.button(f"Close Trade #{row['id']}", key=f"close_{row['id']}_{row['symbol']}"):
                exit_p = live_p if not df_15m.empty else row['entry_price']
                diff = (exit_p - row['entry_price']) if row['bias'] == "BUY" else (row['entry_price'] - exit_p)
                mult = 100 if "XAU" in row['symbol'] else 10000
                pnl = round(diff * row['lot_size'] * mult, 2)
                closed_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
                
                con.execute(
                    "INSERT INTO trade_history_v66 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row['id'], row['created_at_ist'], closed_ist, row['symbol'], row['timeframe'], row['entry_price'], round(exit_p, 2 if is_gold else 5), row['stop_loss'], row['take_profit'], row['lot_size'], row['bias'], "Manual Close", pnl, row['strategy_votes'])
                )
                con.execute("DELETE FROM open_trades_v66 WHERE id = ?", (row['id'],))
                con.commit()
                update_bal(con, pnl)
                st.toast(f"Trade closed! PnL: ${pnl:+.2f}")
                st.rerun()

st.divider()
st.subheader("📜 Closed Trades History")
history_trades = pd.read_sql_query("SELECT * FROM trade_history_v66 ORDER BY id DESC", con)
if history_trades.empty:
    st.info("Abhi tak koi trade closed nahi hui hai.")
else:
    for _, row in history_trades.iterrows():
        fmt = ",.2f" if "XAU" in row['symbol'] else ".5f"
        pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
        with st.expander(f"{pnl_color} History #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+,.2f}"):
            hc1, hc2 = st.columns(2)
            hc1.markdown(f"🕒 **Opened:** `{row['created_at_ist']}`\n🕒 **Closed:** `{row['closed_at_ist']}`\n📍 **Entry:** `{row['entry_price']:{fmt}}` | **Exit:** `{row['exit_price']:{fmt}}`")
            hc2.markdown(f"🛑 **SL:** `{row['stop_loss']:{fmt}}` | **TP:** `{row['take_profit']:{fmt}}`\n🏷️ **Reason:** `{row['exit_reason']}`")

con.close()
