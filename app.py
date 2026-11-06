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
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at_ist TEXT NOT NULL, symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL, entry_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL,
        bias TEXT NOT NULL, strategy_votes TEXT, candle_timestamp TEXT, mt5_ticket INTEGER DEFAULT 0,
        execution_mode TEXT DEFAULT 'PAPER')""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_history_v72(
        id INTEGER PRIMARY KEY, created_at_ist TEXT, closed_at_ist TEXT, symbol TEXT, timeframe TEXT,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL, lot_size REAL, bias TEXT,
        exit_reason TEXT, pnl REAL, strategy_votes TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS account_balance(id INTEGER PRIMARY KEY, balance REAL NOT NULL)")
    
    cols = {r["name"] for r in c.execute("PRAGMA table_info(open_trades_v72)")}
    if "execution_mode" not in cols: 
        c.execute("ALTER TABLE open_trades_v72 ADD COLUMN execution_mode TEXT DEFAULT 'PAPER'")
    
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

@st.cache_data(ttl=300, show_spinner=False)
def cached_twelve_data(sym, interval, apikey):
    url = "https://api.twelvedata.com"
    p = {"symbol": sym, "interval": interval, "outputsize": 100, "timezone": "UTC", "apikey": apikey}
    
    r = None
    for attempt in range(3):
        time.sleep(1.5 * (attempt + 1))
        r = HTTP.get(url + "/time_series", params=p, timeout=15)
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
        raise RuntimeError("Twelve Data rate limit (429) hit or empty response. Please wait 30 seconds.")
        
    d = d[d.index <= pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=mins)]
    
    url = "https://api.twelvedata.com"
    bid = 0.0
    try:
        q = HTTP.get(url + "/price", params={"symbol": sym, "apikey": API_KEY}, timeout=8)
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
        "bear_engulf": a.close < a.open and b.close > b.open and a.open >= b.close and a.close <= b.open,
        "hammer": lower >= 2 * body and upper <= max(body, 0.15 * rng) and a.close > a.open,
        "shooting": upper >= 2 * body and lower <= max(body, 0.15 * rng) and a.close < a.open
    }

def analyze(d, quote, aggressive_mode=False):
    if d is None or len(d) < 30:
        return {"bias": "WAIT", "votes": ["Need completed candles"], "entry": quote, "sl": None, "tp": None, "rr": None, "details": {}}
    
    x = indicators(d).tail(90)
    a, b = x.iloc[-1], x.iloc[-2]
    ph, pl = pivots(x)
    highs = [v for _, v in ph[-4:]]
    lows = [v for _, v in pl[-4:]]
    sh = max(highs) if highs else float(x.high.tail(30).max())
    slw = min(lows) if lows else float(x.low.tail(30).min())
    eq = (sh + slw) / 2
    span = max(sh - slw, 1e-12)
    pat = candle_signals(a, b)
    bull, bear, wait = [], [], []
    
    # Structure & Momentum
    trend = "RANGE"
    if len(ph) >= 2 and len(pl) >= 2:
        if ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]: 
            trend = "UPTREND"
        elif ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]: 
            trend = "DOWNTREND"
            
    if a.close > a.ema20: 
        bull.append("S1/S2 Price above EMA20 (Bullish Momentum)")
    if a.close < a.ema20: 
        bear.append("S1/S2 Price below EMA20 (Bearish Momentum)")
        
    if a.close < a.ema20 and a.ema20 < a.ema50:
        bear.append("S4 Bearish EMA alignment (EMA20 < EMA50)")
    elif a.close > a.ema20 and a.ema20 > a.ema50:
        bull.append("S4 Bullish EMA alignment (EMA20 > EMA50)")

    # S7 Premium / Discount Check
    premium = quote >= eq
    discount = quote < eq
    if premium: 
        bear.append("S7 Premium Zone (Favorable for Sell setups)")
    if discount: 
        bull.append("S7 Discount Zone (Favorable for Buy setups)")

    # Candle Patterns & Retests
    if pat["bear_engulf"] or pat["shooting"]:
        bear.append("S3/S4 Bearish candle rejection pattern")
    if pat["bull_engulf"] or pat["hammer"]:
        bull.append("S3/S4 Bullish candle confirmation pattern")

    bs, ss = len(bull), len(bear)
    bias = "WAIT"
    
    # Decision Engine: If aggressive mode is enabled, respect immediate momentum/structure
    if aggressive_mode:
        if ss > bs or (a.close < a.ema20 and premium):
            bias = "SELL"
        elif bs > ss or (a.close > a.ema20 and discount):
            bias = "BUY"
    else:
        # Standard intelligent rule
        if ss >= 2 and a.close < a.ema20:
            bias = "SELL"
        elif bs >= 2 and a.close > a.ema20:
            bias = "BUY"

    entry = float(quote)
    stop = target = rr = None
    atr = float(a.atr) if pd.notna(a.atr) and a.atr > 0 else span * 0.02
    pad = max(atr * 0.30, span * 0.005)

    if bias == "BUY":
        stop = min(slw, float(x.low.tail(8).min())) - pad
        risk = entry - stop
        target = entry + (risk * MIN_RR)
        rr = (target - entry) / risk if risk > 0 else None
    elif bias == "SELL":
        stop = max(sh, float(x.high.tail(8).max())) + pad
        risk = stop - entry
        target = entry - (risk * MIN_RR)
        rr = (entry - target) / risk if risk > 0 else None
        
    return {
        "bias": bias, "votes": bull + bear + wait, "entry": entry, "sl": stop, "tp": target, "rr": rr,
        "details": {
            "trend": trend, "premium_discount": "PREMIUM" if premium else "DISCOUNT",
            "swing_high": sh, "swing_low": slw, "equilibrium": eq,
            "bull": bull, "bear": bear, "wait": wait
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
    c.execute("""INSERT INTO open_trades_v72(created_at_ist, symbol, timeframe, entry_price, stop_loss,
        take_profit, lot_size, bias, strategy_votes, candle_timestamp, mt5_ticket, execution_mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), asset, tf, r["entry"], r["sl"], r["tp"],
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
st.caption("Verified quotes • completed candles • S1–S9 strategy framework • IST")

c = db_open()

with st.sidebar:
    st.header("🎛️ Terminal Controls")
    if st.button("🔄 Refresh Market Data"): 
        st.rerun()
    asset = st.selectbox("Asset", list(ASSETS))
    tf = st.selectbox("Chart Timeframe", list(TFS), index=1)
    provider = st.selectbox("Price Provider", ["Auto (MT5 first)", "MetaTrader 5", "Twelve Data"])
    
    st.divider()
    aggressive_mode = st.toggle("⚡ Active Trend-Following Mode", value=True, help="When enabled, bot analyzes chart momentum & premium/discount zones to catch active Sell/Buy moves instead of waiting for strict multi-family filters.")
    
    lot = st.number_input("Lot Size", 0.01, 10.0, 0.01, 0.01)
    st.divider()
    mode = st.radio("Execution Mode", ["PAPER TRADING (Demo)", "MT5 LIVE BROKER"], index=0)
    live_ok = st.checkbox("I confirm real broker orders in MT5 LIVE mode", False)
    auto = st.toggle("Auto-pilot", False)
    auto_live = st.checkbox("Also authorize live auto-pilot orders", False)
    if st.button("🔄 Reset Paper Balance ($3,000)"):
        c.execute("UPDATE account_balance SET balance=3000 WHERE id=1")
        c.commit()
        st.rerun()
    if st.button("➕ Execute Current Signal"): 
        st.session_state["execute_now"] = True

try:
    with st.spinner("Analyzing chart structure & price action..."):
        chart, bid, ask, source, mt5_info = market_data(asset, tf, provider)
        d15 = cached_twelve_data(ASSETS[asset]["td"], TFS[tf][2], API_KEY)
        dd = cached_twelve_data(ASSETS[asset]["td"], "1day", API_KEY)
        if d15.empty:
            raise RuntimeError("Rate limit or connection timeout fetching chart data.")
except Exception as e:
    st.error("LIVE FEED UNAVAILABLE — Please wait 30 seconds and click 'Refresh Market Data'.")
    st.code(str(e))
    st.stop()

if len(d15) < 30:
    st.error("Not enough completed candles from provider; no signal.")
    st.stop()

# Run chart analysis with dynamic active mode
r15 = analyze(d15, bid, aggressive_mode=aggressive_mode)
final = r15["bias"]

bal = float(c.execute("SELECT balance FROM account_balance WHERE id=1").fetchone()["balance"])
m1, m2, m3, m4 = st.columns(4)
m1.metric("Provider Live Bid", price_fmt(bid, asset))
m2.metric("Provider Ask", price_fmt(ask, asset))
m3.metric("Paper Balance", f"${bal:,.2f}")
m4.metric("Active Signal", final)
st.caption(f"Feed: {source} · Real-time page chart analysis active.")

if final == "BUY": 
    st.success("🟢 BUY Candidate Detected")
elif final == "SELL": 
    st.error("🔴 SELL Candidate Detected (Active Trend Move)")
else: 
    st.warning("🟡 WAIT — Market consolidating")

det = r15["details"]
with st.expander("📊 Price Lesson Strategy Page Chart Analysis", expanded=True):
    st.write(f"**Structure Trend:** {det.get('trend')} · **Zone:** {det.get('premium_discount')}")
    st.write(f"**Swing Range:** {price_fmt(det.get('swing_low', bid), asset)} to {price_fmt(det.get('swing_high', bid), asset)} · **Equilibrium (50%):** {price_fmt(det.get('equilibrium', bid), asset)}")
    st.markdown("**Bullish Signals Found on Chart:**")
    for x in det.get("bull", []): st.write("🟢 " + x)
    st.markdown("**Bearish Signals Found on Chart:**")
    for x in det.get("bear", []): st.write("🔴 " + x)

if st.session_state.get("execute_now", False):
    st.session_state["execute_now"] = False
    if final not in ["BUY", "SELL"] or r15["sl"] is None: 
        st.warning("No valid trade plan generated; cannot execute.")
    elif mode == "MT5 LIVE BROKER" and not live_ok: 
        st.error("Confirm live execution first.")
    else:
        ts = str(d15.index[-1])
        ticket = 0
        env = "PAPER"
        if mode == "MT5 LIVE BROKER":
            ok, ticket, msg = send_live_order(asset, final, lot, r15["sl"], r15["tp"])
            if not ok: 
                st.error(msg)
                st.stop()
            env = "LIVE"
        open_trade(c, asset, tf, r15, lot, ts, env, ticket)
        st.success(f"{env} trade recorded successfully.")
        st.rerun()

st.divider()
left, right = st.columns([1.5, 1])
with left:
    st.subheader("📈 TradingView Chart (Visual Reference)")
    tv = ASSETS[asset]["tv"]
    tvint = TFS[tf][3]
    html = f'<iframe src="https://s.tradingview.com/widgetembed/?symbol={tv}&interval={tvint}&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" width="100%" height="450" style="border:0"></iframe>'
    components.html(html, height=460, scrolling=False)
with right:
    st.subheader("📋 Active Trade Plan")
    if r15["bias"] in ["BUY", "SELL"] and r15["sl"] is not None:
        st.write("**Direction:** " + r15["bias"])
        st.write("**Entry Reference:** " + price_fmt(r15["entry"], asset))
        st.write("**Stop Loss:** " + price_fmt(r15["sl"], asset))
        st.write("**Take Profit:** " + price_fmt(r15["tp"], asset))
        if r15["rr"] is not None: 
            st.write(f"**Target R:R:** {r15['rr']:.2f}:1")
    else: 
        st.info("Waiting for clean breakout/momentum direction.")

st.divider()
st.subheader("📂 Open Trades")
rows = c.execute("SELECT * FROM open_trades_v72 ORDER BY id DESC").fetchall()
if not rows: 
    st.info("No open trades.")
for row in rows:
    with st.expander(f"#{row['id']} {row['symbol']} {row['bias']} [{row['execution_mode']}]"):
        st.write(f"Opened {row['created_at_ist']} · Entry {row['entry_price']} · SL {row['stop_loss']} · TP {row['take_profit']} · Lot {row['lot_size']}")
        if st.button(f"Close Paper Trade #{row['id']}", key=f"close_{row['id']}"):
            pnl = paper_pnl(row, bid, row["symbol"])
            c.execute("""INSERT INTO trade_history_v72 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["created_at_ist"], now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST"), row["symbol"], row["timeframe"], row["entry_price"], bid, row["stop_loss"], row["take_profit"], row["lot_size"], row["bias"], "Manual paper close", pnl, row["strategy_votes"]))
            c.execute("DELETE FROM open_trades_v72 WHERE id=?", (row["id"],))
            c.execute("UPDATE account_balance SET balance=balance+? WHERE id=1", (pnl,))
            c.commit()
            st.rerun()

c.close()
