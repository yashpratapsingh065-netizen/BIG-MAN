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

# Try importing MetaTrader5
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
try:
    DATA_DIR.mkdir(exist_ok=True)
    _probe = DATA_DIR / ".write_test"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink(missing_ok=True)
    DB_PATH = DATA_DIR / "big_man1.db"
except OSError:
    DB_PATH = Path("/tmp") / "big_man1.db"

st.set_page_config(page_title="BIG MAN1 — Forex AI Pro (SMC & Price Action)", page_icon="📈", layout="wide")

# ==========================================
# EXPERT UI: PREMIUM DARK MODE & BRANDING CSS
# ==========================================
st.markdown("""
<style>
    .stApp {
        background-color: #0b0f19;
        color: #f3f4f6;
    }
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
    div[data-testid="stMetric"] {
        background: linear-gradient(145deg, #111827 0%, #0f172a 100%);
        border: 1px solid #1e293b;
        padding: 16px;
        border-radius: 14px;
        box-shadow: 0 4px 15px -3px rgba(0, 0, 0, 0.5);
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

st.markdown("""
<div class="brand-header">
    <div class="brand-logo">🐂⚡</div>
    <div>
        <div class="brand-title">BIG MAN1 — Forex AI Pro (SMC & Price Action)</div>
        <div class="brand-subtitle">Demo Paper Trading Simulator + MT5 Live Integration & Advanced SMC (FVG, IDM, Supply/Demand)</div>
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
            "mt5_live": "0",
            "last_processed_candles": "{}",
            "last_trade_candles": "{}",
        }
        for key, value in defaults.items():
            existing = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if not existing:
                con.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, value))

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
