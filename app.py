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
    .stTabs
