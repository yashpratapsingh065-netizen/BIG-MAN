import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

try:
    import MetaTrader5 as mt5
    MT5_OK = True
except ImportError:
    mt5 = None
    MT5_OK = False

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.getenv("BIGMAN_DB_PATH", "bigman_price_lesson.db")
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "BIGMAN-PriceLesson/1.0"

st.set_page_config(
    page_title="BIGMAN | Price Lesson SMC Terminal Pro", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""<style>
.stApp { background: #0b0f19; color: #f3f4f6; font-family: 'Inter', sans-serif; }
h1, h2, h3 { color: white; font-family: 'Inter', sans-serif; }
.bh { font-size: 2.2rem; font-weight: 850; background: linear-gradient(90deg, #60a5fa, #34d399);
-webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.stButton>button { width: 100%; font-weight: 650; border-radius: 8px; background-color: #2563eb; color: white; border: none; }
.stButton>button:hover { background-color: #1d4ed8; }
</style>""", unsafe_allow_html=True)

ASSETS = {
    "XAU/USD (Gold)": {"mt5": os.getenv("BIGMAN_MT5_XAU", "XAUUSD"), "td": "XAU/USD", "tv": "OANDA:XAUUSD", "digits": 2, "contract": 100.0},
    "EUR/USD": {"mt5": os.getenv("BIGMAN_MT5_EURUSD", "EURUSD"), "td": "EUR/USD", "tv": "FX:EURUSD", "digits": 5, "contract": 100000.0},
    "GBP/USD": {"mt5": os.getenv("BIGMAN_MT5_GBPUSD", "GBPUSD"), "td": "GBP/USD", "tv": "FX:GBPUSD", "digits": 5, "contract": 100000.0},
}

TFS = {
    "5 min": (5, "TIMEFRAME_M5", "5min", "5"),
    "15 min": (15, "TIMEFRAME_M15", "15min", "15"),
    "1 hour": (60, "TIMEFRAME_H1", "1h", "60"),
    "4 hours": (240, "TIMEFRAME_H4", "4h", "240"),
    "1 day": (1440, "TIMEFRAME_D1", "1day", "D"),
}

MAGIC = 999888
PIVOT = 3
MIN_RR = 2.0

def now_ist(): 
    return datetime.now(IST)

def price_fmt(v, s): 
    return f"{float(v):,.{ASSETS[s]['digits']}f}"

def db_open():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS open_trades_v72(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, created_timestamp REAL NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, 
        lot_size REAL, bias TEXT NOT NULL, strategy_votes TEXT, candle_timestamp TEXT, mt5_ticket INTEGER DEFAULT 0,
        execution_mode TEXT DEFAULT 'PAPER')""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_v72(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    
    cols = {r["name"] for r in c.execute("PRAGMA table_info(open_trades_v72)")}
    if "execution_mode" not in cols: 
        c.execute("ALTER TABLE open_trades_v72 ADD COLUMN execution_mode TEXT DEFAULT 'PAPER'")
    if "created_timestamp" not in cols:
        c.execute("ALTER TABLE open_trades_v72 ADD COLUMN created_timestamp REAL DEFAULT 0")
        
    c.execute("INSERT OR IGNORE INTO account_balance VALUES(1, 3000.0)")
    c.commit()
    return c

def mt5_init():
    if not MT5_OK: 
        raise RuntimeError("MetaTrader5 Python package is not installed here.")
    if not mt5.initialize(): 
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return True

def mt5_data(asset, tf, n=250):
    mt5_init()
    sym = ASSETS[asset]["mt5"]
    if not mt5.symbol_select(sym, True): 
        raise RuntimeError(f"MT5 cannot select {sym}; check broker suffix/Market Watch.")
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    if info is None or tick is None: 
        raise RuntimeError(f"No broker quote for {sym}.")
    mins, tf_attr, _, _ = TFS[tf]
    raw = mt5.copy_rates_from_pos(sym, getattr(mt5, tf_attr), 0, n + 2)
    if raw is None or len(raw) < 65: 
        raise RuntimeError(f"Insufficient MT5 candles: {mt5.last_error()}")
    
    d = pd.DataFrame(raw)
    d["datetime"] = pd.to_datetime(d.time, unit="s", utc=True)
    d = d.rename(columns={"tick_volume": "volume"}).set_index("datetime").sort_index()
    d = d.iloc[:-1].copy()
    d["datetime_ist"] = d.index.tz_convert(IST)
    bid, ask = float(tick.bid), float(tick.ask)
    if bid <= 0 or ask <= 0: 
        raise RuntimeError("Invalid MT5 bid/ask.")
    return d.tail(n), bid, bid, f"MetaTrader 5 broker feed ({sym})", info

@st.cache_data(ttl=120, show_spinner=False)
def cached_twelve_data(sym, interval, apikey):
    url = "https://api.twelvedata.com"
    p = {"symbol": sym, "interval": interval, "outputsize": 100, "timezone": "UTC", "apikey": apikey}
    r = None
    for attempt in range(3):
        time.sleep(1.0 * (attempt + 1))
        r = HTTP.get(url + "/time_series", params=p, timeout=12)
        if r.status_code != 429:
            break
    if r is None or r.status_code == 429:
        return pd.DataFrame()
    r.raise_for_status()
    j = r.json()
    if "values" not in j: 
        return pd.DataFrame()
    
    d = pd.DataFrame(j["values"])
    d["datetime"] = pd.to_datetime(d.datetime, utc=True)
    for k in ["open", "high", "low", "close"]:
        d[k] = pd.to_numeric(d[k], errors="coerce")
    if "volume" not in d: 
        d["volume"] = 0
    d["volume"] = pd.to_numeric(d.volume, errors="coerce").fillna(0)
    d = d.dropna(subset=["open", "high", "low", "close"]).sort_values("datetime").set_index("datetime")
    d["datetime_ist"] = d.index.tz_convert(IST)
    return d

def twelve_data(asset, tf, n=250):
    if not API_KEY: 
        raise RuntimeError("TWELVE_DATA_API_KEY missing.")
    mins, _, interval, _ = TFS[tf]
    sym = ASSETS[asset]["td"]
    d = cached_twelve_data(sym, interval, API_KEY)
    if d.empty:
        raise RuntimeError("Twelve Data rate limit (429) hit or empty response.")
    d = d[d.index <= pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=mins)]
    
    url = "https://api.twelvedata.com"
    bid = 0.0
    try:
        q = HTTP.get(url + "/price", params={"symbol": sym, "apikey": API_KEY}, timeout=6)
        if q.status_code != 429:
            qj = q.json()
            if "price" in qj:
                bid = float(qj["price"])
    except Exception:
        pass
    if bid <= 0 and not d.empty:
        bid = float(d.iloc[-1]["close"])
    if not np.isfinite(bid) or bid <= 0: 
        raise RuntimeError("Invalid Twelve Data price feed.")
    return d.tail(n), bid, bid, f"Twelve Data ({sym})", None

def market_data(asset, tf, provider):
    errors = []
    if provider in ["Auto (MT5 first)", "MetaTrader 5"]:
        try: 
            return mt5_data(asset, tf)
        except Exception as e:
            errors.append("MT5: " + str(e))
            if provider == "MetaTrader 5": 
                raise RuntimeError(" | ".join(errors))
    if provider in ["Auto (MT5 first)", "Twelve Data"]:
        try: 
            return twelve_data(asset, tf)
        except Exception as e: 
            errors.append("Twelve Data: " + str(e))
    raise RuntimeError("No verified live feed. " + " | ".join(errors))

def indicators(d):
    x = d.copy()
    x["ema20"] = x.close.ewm(span=20, adjust=False).mean()
    x["ema50"] = x.close.ewm(span=50, adjust=False).mean()
    tr = pd.concat([x.high - x.low, (x.high - x.close.shift()).abs(), (x.low - x.close.shift()).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(14).mean()
    return x

def pivots(d):
    h, l = [], []
    a = d.high.to_numpy()
    b = d.low.to_numpy()
    for i in range(PIVOT, len(d) - PIVOT):
        if a[i] == np.max(a[i - PIVOT:i + PIVOT + 1]): 
            h.append((i, float(a[i])))
        if b[i] == np.min(b[i - PIVOT:i + PIVOT + 1]): 
            l.append((i, float(b[i])))
    return h, l

def candle_signals(a, b):
    rng = max(float(a.high - a.low), 1e-12)
    body = abs(float(a.close - a.open))
    lower = min(a.open, a.close) - a.low
    upper = a.high - max(a.open, a.close)
    return {
        "doji": body / rng <= 0.12,
        "bull_engulf": a.close > a.open and b.close < b.open and a.open <= b.close and a.close >= b.open,
        "bear_engulf": a.close < a.open and b.close > b.open and a.open >= b.close and a.open <= b.open,
        "hammer": lower >= 2 * body and upper <= max(body, 0.15 * rng) and a.close > a.open,
        "shooting": upper >= 2 * body and lower <= max(body, 0.15 * rng) and a.close < a.open
    }

def analyze_active_execution(d15, quote):
    if d15 is None or len(d15) < 30:
        return {"bias": "WAIT", "votes": ["Insufficient candles"], "entry": quote, "sl": None, "tp": None, "rr": None, "details": {}}
    
    x15 = indicators(d15).tail(60)
    a, b = x15.iloc[-1], x15.iloc[-2]
    ph, pl = pivots(x15)
    sh = float(x15.high.tail(20).max())
    slw = float(x15.low.tail(20).min())
    eq = (sh + slw) / 2
    span = max(sh - slw, 1e-12)
    pat = candle_signals(a, b)
    
    bull, bear = [], []
    
    if a.close > a.ema20:
        bull.append("S1/S2 Momentum: Price is trading above EMA20")
    else:
        bear.append("S1/S2 Momentum: Price is trading below EMA20")
        
    if a.close > a.ema50:
        bull.append("S4 Trend: Bullish alignment with EMA50")
    else:
        bear.append("S4 Trend: Bearish alignment with EMA50")
        
    if quote < eq:
        bull.append("S7 Zone: Discount area favoring Buy/Demand setups")
    else:
        bear.append("S7 Zone: Premium area favoring Sell/Supply setups")
        
    if pat["bull_engulf"] or pat["hammer"]:
        bull.append("S3/S6 Price Action: Bullish candlestick confirmation pattern detected")
    if pat["bear_engulf"] or pat["shooting"]:
        bear.append("S3/S6 Price Action: Bearish candlestick confirmation pattern detected")

    if pat["bull_engulf"] or pat["hammer"] or (a.close > a.ema20 and quote < eq):
        bias = "BUY"
    elif pat["bear_engulf"] or pat["shooting"] or (a.close < a.ema20 and quote >= eq):
        bias = "SELL"
    else:
        bias = "BUY" if a.close >= a.ema20 else "SELL"
        
    entry = float(quote)
    stop = target = rr = None
    atr = float(a.atr) if pd.notna(a.atr) and a.atr > 0 else span * 0.02
    pad = max(atr * 0.25, span * 0.004)

    if bias == "BUY":
        stop = min(slw, float(x15.low.tail(6).min())) - pad
        risk = entry - stop
        target = entry + max(risk * MIN_RR, span * 0.4)
        rr = (target - entry) / risk if risk > 0 else 2.0
    elif bias == "SELL":
        stop = max(sh, float(x15.high.tail(6).max())) + pad
        risk = stop - entry
        target = entry - max(risk * MIN_RR, span * 0.4)
        rr = (entry - target) / risk if risk > 0 else 2.0
        
    return {
        "bias": bias, "votes": bull + bear, "entry": entry, "sl": stop, "tp": target, "rr": rr,
        "details": {
            "trend": "BULLISH" if a.close > a.ema20 else "BEARISH", "premium_discount": "PREMIUM" if quote >= eq else "DISCOUNT",
            "swing_high": sh, "swing_low": slw, "equilibrium": eq,
            "bull": bull, "bear": bear
        }
    }

def send_live_order(asset, bias, lot, sl, tp):
    mt5_init()
    sym = ASSETS[asset]["mt5"]
    if not mt5.symbol_select(sym, True): 
        return False, 0, "Cannot select " + sym
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    if info is None or tick is None: 
        return False, 0, "No broker tick/info"
    typ = mt5.ORDER_TYPE_BUY if bias == "BUY" else mt5.ORDER_TYPE_SELL
    px = float(tick.ask if bias == "BUY" else tick.bid)
    digits = int(info.digits)
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(lot), "type": typ,
        "price": px, "sl": round(float(sl), digits), "tp": round(float(tp), digits), "deviation": 30,
        "magic": MAGIC, "comment": "BIGMAN PriceLesson SMC", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC
    }
    res = mt5.order_send(req)
    if res is None: 
        return False, 0, str(mt5.last_error())
    if res.retcode != mt5.TRADE_RETCODE_DONE: 
        return False, 0, f"Broker rejected: {res.retcode} {res.comment}"
    return True, int(res.order), f"MT5 order #{res.order} accepted"

def open_trade(c, asset, tf, r, lot, ts, mode, ticket=0):
    c.execute("""INSERT INTO open_trades_v72(created_at_ist, created_timestamp, symbol, timeframe, entry_price, stop_loss,
        take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket, execution_mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), time.time(), asset, tf, r["entry"], r["sl"], r["tp"],
         lot, r["bias"], " | ".join(r["votes"]), str(ts), ticket, mode))
    c.commit()

def paper_pnl(row, exit_price, asset):
    contract = ASSETS[asset]["contract"]
    if MT5_OK:
        try:
            mt5_init()
            inf = mt5.symbol_info(ASSETS[asset]["mt5"])
            if inf and inf.trade_contract_size: 
                contract = float(inf.trade_contract_size)
        except Exception: 
            pass
    direction = 1 if row["bias"] == "BUY" else -1
    return round((exit_price - row["entry_price"]) * direction * row["lot_size"] * contract, 2)

# ---------------- STREAMLIT UI ----------------
st.markdown('<div class="bh">BIGMAN ⚡ Price Lesson SMC Terminal Pro</div>', unsafe_allow_html=True)
st.caption("Verified quotes • completed candles • S1–S9 strategy framework • IST • Instant Reversal Engine Active")

c = db_open()

# Automated 15-Minute Trade Expiry Check
open_rows_check = c.execute("SELECT * FROM open_trades_v72").fetchall()
for row in open_rows_check:
    elapsed_mins = (time.time() - row["created_timestamp"]) / 60.0
    if elapsed_mins >= 15.0 and row["execution_mode"] == "PAPER":
        try:
            _, current_bid, _, _, _ = market_data(row["symbol"], "15 min", "Twelve Data")
            pnl = paper_pnl(row, current_bid, row["symbol"])
            c.execute("""INSERT INTO trade_history_v72 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], current_bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "15-Min Auto-Expiry", pnl, row["strategy_votes"]))
            c.execute("DELETE FROM open_trades_v72 WHERE id=?", (row["id"],))
            c.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            c.commit()
        except Exception:
            pass

with st.sidebar:
    st.header("🎛️ Terminal Controls")
    if st.button("🔄 Refresh Market Data"): 
        st.rerun()
    asset = st.selectbox("Asset", list(ASSETS))
    tf = st.selectbox("Chart Timeframe", list(TFS), index=1)
    provider = st.selectbox("Price Provider", ["Auto (MT5 first)", "MetaTrader 5", "Twelve Data"])
    
    lot = st.number_input("Lot Size", 0.01, 10.0, 0.01, 0.01)
    st.divider()
    mode = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    live_ok = st.checkbox("I confirm real broker orders in MT5 LIVE mode", False)
    auto = st.toggle("Auto-pilot (Instant Reversal Active)", True)
    auto_live = st.checkbox("Also authorize live auto-pilot orders", False)
    if st.button("🔄 Reset Paper Balance ($3,000)"):
        c.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        c.commit()
        st.rerun()
    if st.button("➕ Execute Current Signal"): 
        st.session_state["execute_now"] = True

try:
    with st.spinner("Analyzing market momentum & zones..."):
        chart, bid, ask, source, mt5_info = market_data(asset, tf, provider)
        d15 = cached_twelve_data(ASSETS[asset]["td"], "15min", API_KEY)
        if d15.empty: 
            d15 = chart
except Exception as e:
    st.error("LIVE FEED UNAVAILABLE — Please wait 30 seconds and click 'Refresh Market Data'.")
    st.code(str(e))
    st.stop()

r15 = analyze_active_execution(d15, bid)
final = r15["bias"]

bal = float(c.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])
m1, m2, m3, m4 = st.columns(4)
m1.metric("Provider Live Bid", price_fmt(bid, asset))
m2.metric("Provider Ask", price_fmt(ask, asset))
m3.metric("Paper Balance", f"${bal:,.2f}")
m4.metric("Active Signal", final)
st.caption(f"Feed: {source} · Active reversal engine ready for Monday.")

if final == "BUY": 
    st.success("🟢 BUY Signal Active")
elif final == "SELL": 
    st.error("🔴 SELL Signal Active")

det = r15["details"]
with st.expander("📊 Live Strategy Execution Breakdown", expanded=True):
    st.write(f"**Structure Trend:** {det.get('trend')} · **Zone Location:** {det.get('premium_discount')}")
    st.write(f"**Swing Range:** {price_fmt(det.get('swing_low', bid), asset)} to {price_fmt(det.get('swing_high', bid), asset)} · **Equilibrium (50%):** {price_fmt(det.get('equilibrium', bid), asset)}")
    st.markdown("**Bullish Signals:**")
    for x in det.get("bull", []): st.write("🟢 " + x)
    st.markdown("**Bearish Signals:**")
    for x in det.get("bear", []): st.write("🔴 " + x)

# --- INSTANT REVERSAL & AUTO-EXECUTION ENGINE ---
if st.session_state.get("execute_now", False) or auto:
    st.session_state["execute_now"] = False
    if final in ["BUY", "SELL"] and r15["sl"] is not None:
        ts = str(d15.index[-1]) if not d15.empty else str(time.time())
        open_pos = c.execute("SELECT * FROM open_trades_v72 WHERE symbol=?", (asset,)).fetchone()
        
        # If an open position exists but the signal flipped, close early and reverse
        if open_pos and open_pos["bias"] != final:
            pnl = paper_pnl(open_pos, bid, asset)
            c.execute("""INSERT INTO trade_history_v72 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (open_pos["id"], open_pos["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), 
                 asset, tf, open_pos["entry_price"], bid, open_pos["stop_loss"], open_pos["take_profit"], 
                 open_pos["lot_size"], open_pos["bias"], f"Signal Reversal to {final}", pnl, open_pos["strategy_votes"]))
            c.execute("DELETE FROM open_trades_v72 WHERE id=?", (open_pos["id"],))
            c.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            c.commit()
            
            ticket = 0
            env = "PAPER"
            if mode == "MT5 LIVE BROKER" and live_ok and auto_live:
                ok, ticket, msg = send_live_order(asset, final, lot, r15["sl"], r15["tp"])
                if ok: env = "LIVE"
            open_trade(c, asset, tf, r15, lot, ts, env, ticket)
            st.toast(f"Reversed position! Closed #{open_pos['id']} & opened new {final} trade.")
            st.rerun()

        # If no open position exists for this asset, open one
        elif not open_pos:
            exists_candle = c.execute("SELECT id FROM open_trades_v72 WHERE symbol=? AND candle_timestamp=?", (asset, ts)).fetchone()
            if not exists_candle:
                ticket = 0
                env = "PAPER"
                if mode == "MT5 LIVE BROKER" and live_ok and auto_live:
                    ok, ticket, msg = send_live_order(asset, final, lot, r15["sl"], r15["tp"])
                    if ok: env = "LIVE"
                open_trade(c, asset, tf, r15, lot, ts, env, ticket)
                st.toast(f"{env} auto-trade executed for {asset} ({final})!")
                st.rerun()

st.divider()
left, right = st.columns([1.5, 1])
with left:
    st.subheader("📈 TradingView Chart (Visual Reference)")
    tv = ASSETS[asset]["tv"]
    tvint = TFS[tf][3]
    htm