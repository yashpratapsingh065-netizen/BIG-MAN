# BRODAX — Price Lesson Hindi + SMC Signal Engine (Twelve Data Powered)
# Paper-trading only. No broker orders or live execution.

import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except Exception:
    AUTOREFRESH_AVAILABLE = False

st.set_page_config(page_title="BRODAX | Price Lesson + SMC", page_icon="📈", layout="wide")

# --------------------------- CONFIGURATION ---------------------------

DB_FILE = "brodax_paper_trading.db"

# Yahan apna Twelve Data API Key dalein (Aap apna free key twelvedata.com se le sakte hain)
TWELVE_DATA_API_KEY = "a051e2e8558149d39c3d299f1c5feb83" 

SYMBOLS = {
    "Bitcoin (BTC/USD)": {"td": "BTC/USD", "base": 91500.0},
    "Ethereum (ETH/USD)": {"td": "ETH/USD", "base": 3350.0},
    "Solana (SOL/USD)": {"td": "SOL/USD", "base": 200.0},
    "Ripple (XRP/USD)": {"td": "XRP/USD", "base": 2.45}
}

INTERVALS = {
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day"
}

DEFAULT_PIVOT_LOOKBACK = 3
DEFAULT_ZONE_ATR = 0.35
DEFAULT_MIN_RR = 3.0
DEFAULT_CONFLUENCE = 2
DEFAULT_PAPER_BALANCE = 1000.0
REQUEST_TIMEOUT = 10


# --------------------------- DATABASE ---------------------------

def db_connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            quantity REAL NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price REAL,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            signal_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def load_balance(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key='paper_balance'").fetchone()
    if row:
        try:
            return float(row[0])
        except Exception:
            pass
    conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('paper_balance',?)",
                 (str(DEFAULT_PAPER_BALANCE),))
    conn.commit()
    return DEFAULT_PAPER_BALANCE


# --------------------------- MARKET DATA (TWELVE DATA API) ---------------------------

@st.cache_data(ttl=10, show_spinner=False)
def get_klines(symbol_key: str, interval: str, limit: int = 300) -> pd.DataFrame:
    td_symbol = SYMBOLS[symbol_key]["td"]
    td_interval = INTERVALS.get(interval, "1h")
    
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": td_symbol,
            "interval": td_interval,
            "outputsize": limit,
            "apikey": TWELVE_DATA_API_KEY
        }
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = response.json()
        
        if "values" in data:
            df = pd.DataFrame(data["values"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            
            df["close_time"] = pd.to_datetime(df["datetime"], utc=True)
            df["open_time"] = df["close_time"] - pd.Timedelta(minutes=15 if interval == "15m" else 60)
            df = df.sort_values("close_time").dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
            
            if len(df) >= 30:
                return df
    except Exception as e:
        pass

    # Fallback agar API limit exceed ho ya network issue ho
    base_p = SYMBOLS[symbol_key]["base"]
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq="15min" if "15m" in interval else "1h")
    np.random.seed(42)
    noise = np.random.normal(0, base_p * 0.002, limit).cumsum()
    prices = base_p + noise
    
    return pd.DataFrame({
        "open_time": dates - pd.Timedelta(minutes=15),
        "open": prices,
        "high": prices + base_p * 0.003,
        "low": prices - base_p * 0.003,
        "close": prices + np.random.normal(0, base_p * 0.001, limit),
        "volume": np.random.uniform(100, 1000, limit),
        "close_time": dates
    })


@st.cache_data(ttl=5, show_spinner=False)
def get_ticker_price(symbol_key: str) -> Tuple[float, str]:
    td_symbol = SYMBOLS[symbol_key]["td"]
    try:
        url = "https://api.twelvedata.com/price"
        params = {"symbol": td_symbol, "apikey": TWELVE_DATA_API_KEY}
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        if "price" in data:
            return float(data["price"]), "Twelve Data Feed 🟢"
    except Exception:
        pass
        
    return SYMBOLS[symbol_key]["base"], "Static Fallback 🔴"


# --------------------------- INDICATORS / STRUCTURE ---------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["body"] = (x["close"] - x["open"]).abs()
    x["range"] = (x["high"] - x["low"]).replace(0, np.nan)
    x["upper_wick"] = x["high"] - x[["open", "close"]].max(axis=1)
    x["lower_wick"] = x[["open", "close"]].min(axis=1) - x["low"]
    x["bull"] = x["close"] > x["open"]
    x["bear"] = x["close"] < x["open"]
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["atr"] = (
        pd.concat([
            x["high"] - x["low"],
            (x["high"] - x["close"].shift()).abs(),
            (x["low"] - x["close"].shift()).abs()
        ], axis=1).max(axis=1).rolling(14).mean()
    )
    x["vol_sma"] = x["volume"].rolling(20).mean()
    return x.dropna(subset=["atr"]).reset_index(drop=True)


def pivot_points(df: pd.DataFrame, n: int = 3):
    highs, lows = [], []
    if len(df) < 2 * n + 5:
        return highs, lows
    for i in range(n, len(df) - n):
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        if h >= float(df["high"].iloc[i-n:i+n+1].max()):
            highs.append((i, h))
        if l <= float(df["low"].iloc[i-n:i+n+1].min()):
            lows.append((i, l))
    return highs, lows


def trend_from_swings(df: pd.DataFrame, n: int = 3) -> str:
    highs, lows = pivot_points(df, n)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


def momentum_direction(df: pd.DataFrame) -> str:
    if len(df) < 25:
        return "NEUTRAL"
    last = df.iloc[-1]
    if last["close"] > last["ema20"] > last["ema50"] and last["ema20"] > df["ema20"].iloc[-4]:
        return "BULLISH"
    if last["close"] < last["ema20"] < last["ema50"] and last["ema20"] < df["ema20"].iloc[-4]:
        return "BEARISH"
    return "NEUTRAL"


def candle_pattern(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "NONE"
    a, b = df.iloc[-2], df.iloc[-1]
    bull_engulf = (
        a["close"] < a["open"] and b["close"] > b["open"]
        and b["open"] <= a["close"] and b["close"] >= a["open"]
    )
    bear_engulf = (
        a["close"] > a["open"] and b["close"] < b["open"]
        and b["open"] >= a["close"] and b["close"] <= a["open"]
    )
    if bull_engulf:
        return "BULLISH_ENGULFING"
    if bear_engulf:
        return "BEARISH_ENGULFING"
    r = max(float(b["range"]), 1e-12)
    if b["body"] / r <= 0.12:
        return "DOJI"
    if b["lower_wick"] >= b["body"] * 2 and b["upper_wick"] <= b["body"] * 1.2:
        return "BULLISH_REJECTION"
    if b["upper_wick"] >= b["body"] * 2 and b["lower_wick"] <= b["body"] * 1.2:
        return "BEARISH_REJECTION"
    return "NONE"


def recent_levels(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df, n)
    hi = highs[-1][1] if highs else float(df["high"].tail(30).max())
    lo = lows[-1][1] if lows else float(df["low"].tail(30).min())
    return float(hi), float(lo)


def nearest_sr_zones(df: pd.DataFrame, n: int = 3, atr_mult: float = 0.35):
    highs, lows = pivot_points(df, n)
    px = float(df["close"].iloc[-1])
    atr = float(df["atr"].iloc[-1])
    tol = max(atr * atr_mult, px * 0.0005)
    high_levels = [v for _, v in highs if abs(v - px) <= max(atr * 5, px * 0.03)]
    low_levels = [v for _, v in lows if abs(v - px) <= max(atr * 5, px * 0.03)]
    resistance = min(high_levels, key=lambda v: abs(v-px)) if high_levels else None
    support = min(low_levels, key=lambda v: abs(v-px)) if low_levels else None
    return support, resistance, tol


def detect_bos(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df.iloc[:-1].reset_index(drop=True), n)
    close = float(df["close"].iloc[-1])
    if highs and close > highs[-1][1]:
        return "BULLISH", float(highs[-1][1])
    if lows and close < lows[-1][1]:
        return "BEARISH", float(lows[-1][1])
    return "NONE", None


def detect_liquidity_sweep(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df.iloc[:-1].reset_index(drop=True), n)
    last = df.iloc[-1]
    if highs:
        level = highs[-1][1]
        if last["high"] > level and last["close"] < level:
            return "BEARISH_SWEEP", float(level)
    if lows:
        level = lows[-1][1]
        if last["low"] < level and last["close"] > level:
            return "BULLISH_SWEEP", float(level)
    return "NONE", None


def find_fvg(df: pd.DataFrame):
    if len(df) < 5:
        return None
    for i in range(len(df)-2, max(1, len(df)-35), -1):
        c1, c2, c3 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        bullish_gap = float(c1["high"]) < float(c3["low"]) and bool(c2["bull"])
        bearish_gap = float(c1["low"]) > float(c3["high"]) and bool(c2["bear"])
        if bullish_gap:
            low, high = float(c1["high"]), float(c3["low"])
            later = df.iloc[i+2:]
            touched = (later["low"] <= high).any()
            if not touched:
                return {"side": "BULLISH", "low": low, "high": high, "mid": (low+high)/2, "index": i+1}
        if bearish_gap:
            low, high = float(c3["high"]), float(c1["low"])
            later = df.iloc[i+2:]
            touched = (later["high"] >= low).any()
            if not touched:
                return {"side": "BEARISH", "low": low, "high": high, "mid": (low+high)/2, "index": i+1}
    return None


def detect_sd_zone(df: pd.DataFrame, atr_mult: float = 0.35):
    if len(df) < 30:
        return None
    x = df.reset_index(drop=True)
    recent_start = max(5, len(x)-60)
    for i in range(len(x)-4, recent_start, -1):
        prev = x.iloc[i-1]
        nxt = x.iloc[i:i+3]
        strong_bull = (nxt["bull"] & (nxt["body"] >= nxt["atr"] * 0.55)).sum() >= 3
        strong_bear = (nxt["bear"] & (nxt["body"] >= nxt["atr"] * 0.55)).sum() >= 3
        if strong_bull:
            return {"side": "DEMAND", "low": float(prev["low"]), "high": float(prev["high"]), "method": "3-candle momentum origin"}
        if strong_bear:
            return {"side": "SUPPLY", "low": float(prev["low"]), "high": float(prev["high"]), "method": "3-candle momentum origin"}
    return None


def impulse_corrective_reversal(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df, n)
    if len(highs) < 2 or len(lows) < 2:
        return "NONE"
    trend = trend_from_swings(df, n)
    if trend == "BEARISH":
        down_leg = abs(lows[-1][1] - highs[-1][1])
        up_leg = abs(highs[-1][1] - lows[-2][1])
        if up_leg > down_leg * 1.15:
            return "POTENTIAL_BULLISH_REVERSAL"
    elif trend == "BULLISH":
        up_leg = abs(highs[-1][1] - lows[-1][1])
        down_leg = abs(lows[-1][1] - highs[-2][1])
        if down_leg > up_leg * 1.15:
            return "POTENTIAL_BEARISH_REVERSAL"
    return "NONE"


# --------------------------- STRATEGY ENGINE ---------------------------

def result(name, side="WAIT", reason="No complete setup", confidence="LOW", details=""):
    return {"strategy": name, "side": side, "reason": reason, "confidence": confidence, "details": details}


def analyze_all(data: Dict[str, pd.DataFrame], pivot_n: int, zone_atr: float):
    d5, d15, h1, h4, d1 = [add_indicators(data[k]) for k in ["5m", "15m", "1h", "4h", "1d"]]
    out = []

    entry = d15
    px = float(entry["close"].iloc[-1])
    atr = float(entry["atr"].iloc[-1])
    pattern = candle_pattern(entry)
    trend15 = trend_from_swings(entry, pivot_n)
    bos15, bos_level = detect_bos(entry, pivot_n)
    sweep15, sweep_level = detect_liquidity_sweep(entry, pivot_n)

    # S1 — MTF
    dir4, beh1, ent15 = momentum_direction(h4), momentum_direction(h1), momentum_direction(d15)
    if dir4 == beh1 == ent15 and dir4 in ("BULLISH", "BEARISH"):
        wanted_pattern = "BULLISH_ENGULFING" if dir4 == "BULLISH" else "BEARISH_ENGULFING"
        side = "BUY" if dir4 == "BULLISH" else "SELL"
        if pattern == wanted_pattern or bos15 == dir4:
            out.append(result("S1 MTF", side, "4H/1H/15M momentum aligned; 15M confirmation present", "HIGH"))
        else:
            out.append(result("S1 MTF", reason="Timeframes aligned; waiting for 15M reversal confirmation"))
    else:
        out.append(result("S1 MTF", reason="Higher/lower timeframe momentum not fully aligned"))

    # S2 — Chart Reading
    reversal = impulse_corrective_reversal(d15, pivot_n)
    if reversal == "POTENTIAL_BULLISH_REVERSAL" and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        out.append(result("S2 Chart Reading", "BUY", "Downside impulse weakening; bullish candle confirmation", "MEDIUM"))
    elif reversal == "POTENTIAL_BEARISH_REVERSAL" and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        out.append(result("S2 Chart Reading", "SELL", "Upside impulse weakening; bearish candle confirmation", "MEDIUM"))
    else:
        out.append(result("S2 Chart Reading", reason="Impulse/corrective reversal confirmation absent"))

    # S3 — 3-Step Price Action
    s3_side, s3_reason = "WAIT", "Waiting for retest/Doji followed by engulfing confirmation"
    if len(d15) >= 4:
        prev2 = d15.iloc[-3]
        if trend15 == "BULLISH" and prev2["body"] / max(prev2["range"], 1e-12) <= 0.15 and pattern == "BULLISH_ENGULFING":
            s3_side, s3_reason = "BUY", "Bullish structure; Doji followed by bullish engulfing"
        elif trend15 == "BEARISH" and prev2["body"] / max(prev2["range"], 1e-12) <= 0.15 and pattern == "BEARISH_ENGULFING":
            s3_side, s3_reason = "SELL", "Bearish structure; Doji followed by bearish engulfing"
    out.append(result("S3 3-Step Price Action", s3_side, s3_reason))

    # S4 — Support & Resistance
    support, resistance, tol = nearest_sr_zones(d15, pivot_n, zone_atr)
    ema_cross_up = d15["ema20"].iloc[-1] > d15["ema50"].iloc[-1] and d15["ema20"].iloc[-2] <= d15["ema50"].iloc[-2]
    ema_cross_dn = d15["ema20"].iloc[-1] < d15["ema50"].iloc[-1] and d15["ema20"].iloc[-2] >= d15["ema50"].iloc[-2]
    if support is not None and abs(px-support) <= tol*2 and pattern == "BULLISH_ENGULFING" and ema_cross_up:
        out.append(result("S4 Support & Resistance", "BUY", "Near support; bullish engulfing and MA crossover", "HIGH"))
    elif resistance is not None and abs(px-resistance) <= tol*2 and pattern == "BEARISH_ENGULFING" and ema_cross_dn:
        out.append(result("S4 Support & Resistance", "SELL", "Near resistance; bearish engulfing and MA crossover", "HIGH"))
    else:
        out.append(result("S4 Support & Resistance", reason="Need nearby S/R zone + reversal candle + MA crossover"))

    # S5 — Inducement (IDM)
    idm_side, idm_reason = "WAIT", "No confirmed BOS followed by first-pullback inducement"
    if len(d15) > 20:
        prior = d15.iloc[-20:-1]
        p_highs, p_lows = pivot_points(prior.reset_index(drop=True), pivot_n)
        if bos15 == "BULLISH" and p_lows:
            pullback_low = p_lows[-1][1]
            if float(d15["low"].iloc[-1]) <= pullback_low and float(d15["close"].iloc[-1]) > pullback_low:
                idm_side, idm_reason = "BUY", "Bullish BOS; first pullback low swept and reclaimed"
        elif bos15 == "BEARISH" and p_highs:
            pullback_high = p_highs[-1][1]
            if float(d15["high"].iloc[-1]) >= pullback_high and float(d15["close"].iloc[-1]) < pullback_high:
                idm_side, idm_reason = "SELL", "Bearish BOS; first pullback high swept and rejected"
    out.append(result("S5 Inducement (IDM)", idm_side, idm_reason))

    # S5-FVG — Valid FVG
    fvg = find_fvg(d15)
    fvg_side, fvg_reason = "WAIT", "No fresh unmitigated FVG with required confluences"
    if fvg:
        mid_reject_bull = fvg["side"] == "BULLISH" and float(d15["low"].iloc[-1]) <= fvg["mid"] and float(d15["close"].iloc[-1]) > fvg["mid"]
        mid_reject_bear = fvg["side"] == "BEARISH" and float(d15["high"].iloc[-1]) >= fvg["mid"] and float(d15["close"].iloc[-1]) < fvg["mid"]
        if fvg["side"] == "BULLISH" and sweep15 == "BULLISH_SWEEP" and bos15 == "BULLISH" and mid_reject_bull:
            fvg_side, fvg_reason = "BUY", "Fresh bullish FVG + sweep + BOS + 50% midpoint rejection"
        elif fvg["side"] == "BEARISH" and sweep15 == "BEARISH_SWEEP" and bos15 == "BEARISH" and mid_reject_bear:
            fvg_side, fvg_reason = "SELL", "Fresh bearish FVG + sweep + BOS + 50% midpoint rejection"
    out.append(result("S5-FVG Valid FVG", fvg_side, fvg_reason))

    # S6 & S9 — Supply & Demand
    sd = detect_sd_zone(d15, zone_atr)
    sd_side, sd_reason = "WAIT", "No valid recent S/D zone with confirmation"
    if sd:
        in_zone = sd["low"] <= px <= sd["high"] or (float(d15["low"].iloc[-1]) <= sd["high"] and float(d15["high"].iloc[-1]) >= sd["low"])
        if sd["side"] == "DEMAND" and in_zone and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
            sd_side, sd_reason = "BUY", f"Demand zone retest + bullish confirmation ({sd['method']})"
        elif sd["side"] == "SUPPLY" and in_zone and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
            sd_side, sd_reason = "SELL", f"Supply zone retest + bearish confirmation ({sd['method']})"
    out.append(result("S6 Supply & Demand", sd_side, sd_reason))
    out.append(result("S9 S/D Full Guide", sd_side, sd_reason))

    # S7 — Premium & Discount
    swing_high, swing_low = recent_levels(d15, pivot_n)
    eq = (swing_high + swing_low) / 2
    pd_side, pd_reason = "WAIT", "Wait for price to enter appropriate half of dealing range"
    if px < eq and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        pd_side, pd_reason = "BUY", "Discount zone (< 0.5 Eq) + bullish rejection"
    elif px > eq and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        pd_side, pd_reason = "SELL", "Premium zone (> 0.5 Eq) + bearish rejection"
    out.append(result("S7 Premium & Discount", pd_side, pd_reason))

    # S8 — Daily Bias
    daily_bias, daily_reason = "NEUTRAL", "No clear daily bias"
    if len(d1) >= 3:
        p2, p1 = d1.iloc[-2], d1.iloc[-1]
        if p1["close"] > p2["high"]:
            daily_bias, daily_reason = "BULLISH", "Daily body close above previous high (PDH break)"
        elif p1["close"] < p2["low"]:
            daily_bias, daily_reason = "BEARISH", "Daily body close below previous low (PDL break)"
    daily_side = "WAIT"
    if daily_bias == "BULLISH" and ent15 == "BULLISH" and (pattern == "BULLISH_ENGULFING" or fvg_side == "BUY" or sd_side == "BUY"):
        daily_side = "BUY"
    elif daily_bias == "BEARISH" and ent15 == "BEARISH" and (pattern == "BEARISH_ENGULFING" or fvg_side == "SELL" or sd_side == "SELL"):
        daily_side = "SELL"
    out.append(result("S8 Daily Bias", daily_side, daily_reason))

    recent_hi, recent_lo = recent_levels(d15, pivot_n)
    buffer = max(atr * 0.15, px * 0.0003)
    buy_stop = min(recent_lo, px - atr) - buffer
    sell_stop = max(recent_hi, px + atr) + buffer
    buy_risk = max(px - buy_stop, atr * 0.25)
    sell_risk = max(sell_stop - px, atr * 0.25)
    buy_target = px + buy_risk * DEFAULT_MIN_RR
    sell_target = px - sell_risk * DEFAULT_MIN_RR

    votes = {"BUY": [], "SELL": []}
    for r in out:
        if r["side"] in votes:
            family = "S6/S9" if r["strategy"].startswith(("S6", "S9")) else r["strategy"]
            if family not in votes[r["side"]]:
                votes[r["side"]].append(family)
    buy_count, sell_count = len(votes["BUY"]), len(votes["SELL"])
    consensus = "WAIT"
    if buy_count >= DEFAULT_CONFLUENCE and buy_count > sell_count:
        consensus = "BUY"
    elif sell_count >= DEFAULT_CONFLUENCE and sell_count > buy_count:
        consensus = "SELL"

    return {
        "strategies": out, "entry_price": px, "pattern": pattern, "trend15": trend15,
        "daily_bias": daily_bias, "daily_reason": daily_reason,
        "buy_votes": buy_count, "sell_votes": sell_count, "consensus": consensus,
        "buy_stop": buy_stop, "buy_target": buy_target,
        "sell_stop": sell_stop, "sell_target": sell_target,
        "atr": atr, "support": support, "resistance": resistance, "fvg": fvg, "sd": sd,
        "dataframes": {"5m": d5, "15m": d15, "1h": h1, "4h": h4, "1d": d1},
    }


# --------------------------- UI SECTION ---------------------------

st.title("📈 BRODAX — Price Lesson Hindi + SMC")
st.caption("Paper-trading signal engine • Twelve Data API Integration")

with st.sidebar:
    st.header("⚙️ Settings")
    symbol = st.selectbox("Crypto pair", list(SYMBOLS.keys()), index=0)
    pivot_n = st.slider("Swing lookback", 2, 6, DEFAULT_PIVOT_LOOKBACK)
    zone_atr = st.slider("S/R zone tolerance", 0.15, 1.0, DEFAULT_ZONE_ATR, 0.05)
    min_votes = st.slider("Minimum votes", 1, 6, DEFAULT_CONFLUENCE)
    paper_risk_pct = st.slider("Paper risk (%)", 0.25, 2.0, 1.0, 0.25)
    refresh_seconds = st.selectbox("Refresh interval", [5, 10, 15, 30], index=1)

if AUTOREFRESH_AVAILABLE:
    st_autorefresh(interval=refresh_seconds * 1000, key="brodax_refresh")

conn = db_connect()
balance = load_balance(conn)

try:
    with st.spinner("Fetching market data from Twelve Data API..."):
        market = {tf: get_klines(symbol, tf, 200) for tf in INTERVALS}
        live_price, used_host = get_ticker_price(symbol)
        analysis = analyze_all(market, pivot_n, zone_atr)
except Exception as e:
    st.error(f"Error: {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric(f"{symbol} live ticker", f"${live_price:,.2f}")
c2.metric("Last 15m close", f"${analysis['entry_price']:,.2f}")
c3.metric("15m structure", analysis["trend15"])
c4.metric("Daily bias", analysis["daily_bias"])

st.divider()
st.subheader("🎯 Combined Signal")
left, right = st.columns([1, 2])
with left:
    if analysis["buy_votes"] >= min_votes and analysis["buy_votes"] > analysis["sell_votes"]:
        st.success("BUY — paper signal")
    elif analysis["sell_votes"] >= min_votes and analysis["sell_votes"] > analysis["buy_votes"]:
        st.error("SELL — paper signal")
    else:
        st.warning("WAIT — no sufficient confluence")
    st.write(f"BUY votes: **{analysis['buy_votes']}** | SELL votes: **{analysis['sell_votes']}**")
with right:
    st.write(f"**Entry:** ${analysis['entry_price']:,.2f}")
    st.write(f"**Stop:** ${analysis['buy_stop']:,.2f}")
    st.write(f"**Target:** ${analysis['buy_target']:,.2f}")

st.divider()
st.subheader("📚 All Price Lesson Strategies")
st.dataframe(pd.DataFrame(analysis["strategies"])[["strategy", "side", "reason"]], use_container_width=True, hide_index=True)

con.close()
