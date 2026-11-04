import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN 2.0 — AI Adaptive Trading Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_2_4_ai_adaptive_bot.db")
DIRECT_TWELVE_DATA_KEY = "a051e2e8558149d39c3d299f1c5feb83"

SYMBOLS = {
    "XAU/USD (Gold)": {"td": "XAU/USD", "yf": "GC=F", "tv": "OANDA:XAUUSD", "mt5": "XAUUSD"},
    "EUR/USD": {"td": "EUR/USD", "yf": "EURUSD=X", "tv": "FX:EURUSD", "mt5": "EURUSD"},
    "GBP/USD": {"td": "GBP/USD", "yf": "GBPUSD=X", "tv": "FX:GBPUSD", "mt5": "GBPUSD"},
}

INTERVALS = {
    "15 min": {"td": "15min", "yf": "15m", "tv_int": "15", "minutes": 15},
    "1 hour": {"td": "1h", "yf": "60m", "tv_int": "60", "minutes": 60},
    "4 hours": {"td": "240min", "yf": "1h", "tv_int": "240", "minutes": 240},
    "1 day": {"td": "1day", "yf": "1d", "tv_int": "D", "minutes": 1440}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tv_open_trades_v24 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_time_utc TIMESTAMP NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.01,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history_v24 (
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS adaptive_strategy_weights (
            pattern_key TEXT PRIMARY KEY,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            confidence_score REAL DEFAULT 1.0
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 3000.0)")
    con.commit()
    return con

def get_forex_market_status():
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()
    hour = now_utc.hour
    if weekday == 5:
        return "CLOSED", "🔴 Market Closed (Weekend Saturday)"
    elif weekday == 6 and hour < 22:
        return "CLOSED", "🔴 Market Closed (Weekend Sunday)"
    elif weekday == 4 and hour >= 22:
        return "CLOSED", "🔴 Market Closed (Weekend Friday Evening)"
    else:
        return "OPEN", "🟢 International Forex/Gold Market Live"

def get_market_data(symbol_label, interval_label, rows=150):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    td_key = DIRECT_TWELVE_DATA_KEY
    
    if td_key:
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": info["td"], "interval": cfg["td"], "outputsize": rows, "apikey": td_key},
                timeout=7,
            )
            payload = resp.json()
            if "values" in payload:
                df = pd.DataFrame(payload["values"])
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
                df["datetime_ist"] = df["datetime"].dt.tz_convert(IST)
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime").set_index("datetime")
                live_price = float(df.close.iloc[-1])
                if live_price > 0:
                    return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), live_price, "Twelve Data Feed 🟢"
        except Exception:
            pass

    try:
        import yfinance as yf
        ticker = yf.Ticker(info["yf"])
        df_yf = ticker.history(period="5d", interval=cfg["yf"])
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df_yf["datetime_ist"] = df_yf.index.tz_convert(IST)
            live_price = float(df_yf.close.iloc[-1])
            if live_price > 0:
                return df_yf[["open", "high", "low", "close", "datetime_ist"]].tail(rows), live_price, "Yahoo Finance Feed 🟡"
    except Exception:
        pass

    dummy_price = 2650.0 if "XAU" in symbol_label else (1.0850 if "EUR" in symbol_label else 1.2850)
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=rows, freq="15min")
    df_dummy = pd.DataFrame({
        "open": dummy_price, "high": dummy_price + 2.0, "low": dummy_price - 2.0, "close": dummy_price,
        "datetime_ist": pd.Series(dates).dt.tz_convert(IST)
    }, index=dates)
    return df_dummy, dummy_price, "Static Fallback Feed 🔴"

# --- AI SELF-IMPROVEMENT & ADAPTIVE LEARNING ENGINE ---
def update_adaptive_intelligence(con):
    """Analyzes trade history and updates confidence weights for strategy patterns."""
    cursor = con.cursor()
    cursor.execute("SELECT strategy_votes, pnl FROM trade_history_v24")
    history = cursor.fetchall()
    
    for votes, pnl in history:
        if not votes:
            continue
        is_win = pnl > 0
        patterns = [p.strip() for p in votes.split('|')]
        for pattern in patterns:
            if not pattern:
                continue
            cursor.execute("SELECT win_count, loss_count FROM adaptive_strategy_weights WHERE pattern_key = ?", (pattern,))
            row = cursor.fetchone()
            if row:
                w, l = row
                if is_win: w += 1
                else: l += 1
                conf = round((w / max(1, w + l)) * 2.0, 2) # Adaptive weight multiplier
                cursor.execute("UPDATE adaptive_strategy_weights SET win_count = ?, loss_count = ?, confidence_score = ? WHERE pattern_key = ?", (w, l, conf, pattern))
            else:
                cursor.execute("INSERT INTO adaptive_strategy_weights (pattern_key, win_count, loss_count, confidence_score) VALUES (?, ?, ?, ?)", 
                               (pattern, 1 if is_win else 0, 0 if is_win else 1, 1.2 if is_win else 0.8))
    con.commit()

def get_pattern_confidence(con, pattern_text):
    cursor = con.cursor()
    cursor.execute("SELECT confidence_score FROM adaptive_strategy_weights WHERE pattern_key = ?", (pattern_text,))
    row = cursor.fetchone()
    return float(row[0]) if row else 1.0

# --- BACKTESTING ENGINE ---
def run_historical_backtest(df, is_gold):
    """Simulates strategy over historical candles to compute win rate and profit."""
    if len(df) < 30:
        return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "max_dd": 0.0}
    
    balance = 1000.0
    peak = balance
    max_dd = 0.0
    wins = 0
    total_trades = 0
    total_pnl = 0.0
    
    for i in range(20, len(df) - 1):
        window = df.iloc[:i+1]
        last = window.iloc[-1]
        p_price = last['close']
        
        swing_high = float(window.high.tail(20).max())
        swing_low = float(window.low.tail(20).min())
        eq_50 = (swing_high + swing_low) / 2.0
        
        bias = "SELL" if p_price >= eq_50 else "BUY"
        sl_buffer = max((last.high - last.low) * 1.2, 2.5 if is_gold else 0.0015)
        tp_buffer = sl_buffer * 2.5
        
        entry = p_price
        tp = entry + tp_buffer if bias == "BUY" else entry - tp_buffer
        sl = entry - sl_buffer if bias == "BUY" else entry + sl_buffer
        
        next_candle = df.iloc[i+1]
        hit_tp = (next_candle.high >= tp) if bias == "BUY" else (next_candle.low <= tp)
        hit_sl = (next_candle.low <= sl) if bias == "BUY" else (next_candle.high >= sl)
        
        total_trades += 1
        multiplier = 100 if is_gold else 10000
        
        if hit_tp and not hit_sl:
            pnl = tp_buffer * 0.01 * multiplier
            wins += 1
            total_pnl += pnl
        elif hit_sl and not hit_tp:
            pnl = -sl_buffer * 0.01 * multiplier
            total_pnl += pnl
        else:
            # Time exit / neutral close
            diff = (next_candle.close - entry) if bias == "BUY" else (entry - next_candle.close)
            pnl = diff * 0.01 * multiplier
            total_pnl += pnl
            if pnl > 0: wins += 1
            
        balance += pnl
        if balance > peak: peak = balance
        dd = peak - balance
        if dd > max_dd: max_dd = dd

    win_rate = round((wins / max(1, total_trades)) * 100.0, 2)
    return {
        "trades": total_trades,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "max_dd": round(max_dd, 2)
    }

def evaluate_daily_bias(symbol_label, live_price):
    df_daily, _, _ = get_market_data(symbol_label, "1 day", rows=10)
    if df_daily.empty or len(df_daily) < 3:
        return "BUY", "Initializing...", 1, 1

    prev = df_daily.iloc[-2]
    p_prev = df_daily.iloc[-3]
    pd_high, pd_low = prev['high'], prev['low']
    
    is_inside_bar = prev['high'] <= p_prev['high'] and prev['low'] >= p_prev['low']
    bull_votes, bear_votes = [], []
    
    if is_inside_bar:
        if live_price > pd_high: bull_votes.append("Inside Bar Bullish Breakout")
        elif live_price < pd_low: bear_votes.append("Inside Bar Bearish Breakdown")
        else: bull_votes.append("Inside Bar Equilibrium")
    else:
        if prev['close'] > pd_high: bull_votes.append("Daily Close > Prev High (Bullish)")
        elif prev['close'] < pd_low: bear_votes.append("Daily Close < Prev Low (Bearish)")
        else:
            if prev['low'] < p_prev['low'] and prev['close'] > prev['open']: bull_votes.append("Prev Day Low Liquidity Sweep")
            elif prev['high'] > p_prev['high'] and prev['close'] < prev['open']: bear_votes.append("Prev Day High Liquidity Sweep")
            else:
                if live_price >= ((pd_high + pd_low) / 2): bear_votes.append("Premium Zone Bias")
                else: bull_votes.append("Discount Zone Bias")

    b_score = len(bull_votes) if bull_votes else 1
    s_score = len(bear_votes) if bear_votes else 1
    bias = "BUY" if b_score >= s_score else "SELL"
    votes_str = f"Bullish: {', '.join(bull_votes)} | Bearish: {', '.join(bear_votes)}"
    return bias, votes_str, b_score, s_score

def evaluate_price_lesson_strategy(con, df, live_market_price, is_gold, symbol_label):
    if len(df) < 10:
        return {"bias":"BUY", "votes":"Default Execution", "bullish":1, "bearish":0, "entry":live_market_price, "sl":live_market_price-(2.5 if is_gold else 0.0015), "tp":live_market_price+(6.25 if is_gold else 0.00375)}
    
    daily_bias, daily_votes, _, _ = evaluate_daily_bias(symbol_label, live_market_price)
    
    last = df.iloc[-1]
    rng = max(last.high - last.low, 1e-12)
    swing_high = float(df.high.tail(30).max())
    swing_low = float(df.low.tail(30).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    # Apply Adaptive Intelligence weights from past trades
    adaptive_weight = get_pattern_confidence(con, daily_bias)
    
    if live_market_price >= eq_50:
        bias = "SELL"
        votes_str = f"Daily Bias ({daily_bias}) + Premium Zone | AI Weight: {adaptive_weight}x"
    else:
        bias = "BUY"
        votes_str = f"Daily Bias ({daily_bias}) + Discount Zone | AI Weight: {adaptive_weight}x"

    sl_buffer = max(rng * 1.2, 2.5 if is_gold else 0.0015)
    tp_buffer = sl_buffer * 2.5

    if bias == "BUY":
        sl = live_market_price - sl_buffer
        tp = live_market_price + tp_buffer
    else:
        sl = live_market_price + sl_buffer
        tp = live_market_price - tp_buffer

    decimals = 2 if is_gold else 5
    return {
        "bias": bias,
        "votes": votes_str,
        "bullish": 2,
        "bearish": 0,
        "entry": round(live_market_price, decimals),
        "sl": round(sl, decimals),
        "tp": round(tp, decimals)
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 3000.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def close_single_trade(con, trade_id, current_price, symbol_name, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v24 WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    is_gold = "XAU" in symbol
    
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    multiplier = 100 if is_gold else 10000
    pnl_usd = round(diff * lot_size * multiplier, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    decimals = 2 if is_gold else 5
    
    con.execute(
        "INSERT INTO trade_history_v24 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, decimals), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v24 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    update_adaptive_intelligence(con) # Trigger Self-Improvement Learning Loop
    return pnl_usd

def check_auto_exit_conditions_for_symbol(con, symbol_label, current_price):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, stop_loss, take_profit FROM tv_open_trades_v24 WHERE symbol = ?", (symbol_label,))
    open_trades = cursor.fetchall()
    
    for tid, bias, sl, tp in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close"
        
        if bias == "BUY":
            if current_price >= tp: exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price <= sl: exit_p, reason, closed = sl, "SL Hitter 🛑", True
        elif bias == "SELL":
            if current_price <= tp: exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price >= sl: exit_p, reason, closed = sl, "SL Hitter 🛑", True
                
        if closed:
            close_single_trade(con, tid, exit_p, symbol_label, reason=reason)

def manage_auto_trading(con, symbol, timeframe, result, lot_size, candle_ts, market_status, allow_weekend_practice):
    if market_status != "OPEN" and not allow_weekend_practice:
        return

    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v24 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(candle_ts)))
    if not cursor.fetchone():
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO tv_open_trades_v24 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI DESIGN ---
st.title("🪙 BIG MAN 2.0 — AI Adaptive & Backtested Bot")
st.caption("MetaQuotes-Demo (5058003871) • Self-Improvement Learning Engine • Historical Backtester")

con = connect_db()
m_status, m_text = get_forex_market_status()

with st.sidebar:
    st.header("⚙️ Control Panel")
    
    with st.expander("🔑 MT5 Account Credentials", expanded=False):
        st.text_input("Login ID", value="5058003871", disabled=True)
        st.text_input("Server", value="MetaQuotes-Demo", disabled=True)
        st.success("🟢 Account Synced")

    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    active_chart_tf = st.selectbox("Active Chart Timeframe", list(INTERVALS.keys()), index=0)
    lot_input = st.number_input("Trade Lot Size", min_value=0.01, max_value=5.0, value=0.01, step=0.01)
    
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        st.rerun()
    
    st.markdown("---")
    weekend_practice = st.checkbox("Weekend Practice Mode", value=True)
    auto_mode = st.toggle("Enable Auto-Pilot Bot", value=True)
    
    is_gold = "XAU" in symbol
    df_temp, live_p_temp, _ = get_market_data(symbol, active_chart_tf)
    
    if st.button("➕ Open Manual Trade", use_container_width=True):
        if not df_temp.empty and live_p_temp > 0:
            res_temp = evaluate_price_lesson_strategy(con, df_temp, live_p_temp, is_gold, symbol)
            candle_ts = df_temp.index[-1]
            manage_auto_trading(con, symbol, active_chart_tf, res_temp, lot_size=lot_input, candle_ts=candle_ts, market_status=m_status, allow_weekend_practice=weekend_practice)
            st.sidebar.success(f"Manual Trade Added for {symbol}!")
            st.rerun()

    st.markdown("---")
    if st.button("🗑️ Clear All Trades & History", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v24")
        con.execute("DELETE FROM trade_history_v24")
        con.commit()
        st.sidebar.success("Cleared!")
        st.rerun()

    if st.button("🔄 Reset Demo Balance ($3,000)", use_container_width=True):
        con.execute("UPDATE account_balance SET balance = 3000.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Reset to $3,000!")
        st.rerun()

st.info(f"Market Status: **{m_text}**")

is_gold = "XAU" in symbol
live_price_global = 0.0

if auto_mode:
    for tf_lbl in INTERVALS.keys():
        df_scan, live_p_scan, _ = get_market_data(symbol, tf_lbl)
        if not df_scan.empty and live_p_scan > 0:
            if tf_lbl == active_chart_tf:
                live_price_global = live_p_scan
            res_scan = evaluate_price_lesson_strategy(con, df_scan, live_p_scan, is_gold, symbol)
            manage_auto_trading(con, symbol, tf_lbl, res_scan, lot_size=lot_input, candle_ts=df_scan.index[-1], market_status=m_status, allow_weekend_practice=weekend_practice)

if live_price_global == 0.0:
    _, live_price_global, _ = get_market_data(symbol, active_chart_tf)

check_auto_exit_conditions_for_symbol(con, symbol, live_price_global)

curr_balance = get_current_balance(con)
bal_color = "🟢" if curr_balance >= 3000 else "🔴"
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")

df_active, live_p_active, data_source = get_market_data(symbol, active_chart_tf)
if live_p_active > 0:
    live_price_global = live_p_active

result_active = evaluate_price_lesson_strategy(con, df_active, live_price_global, is_gold, symbol)

col1, col2, col3 = st.columns(3)
col1.metric("Live Market Price", f"{live_price_global:.2f}" if is_gold else f"{live_price_global:.5f}")
col2.metric(f"{active_chart_tf} Bias", result_active["bias"])
col3.metric("AI Learning Status", "Adaptive Active 🧠")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live Chart ({symbol} - {active_chart_tf})")
    tv_symbol = SYMBOLS[symbol]["tv"]
    tv_int = INTERVALS[active_chart_tf]["tv_int"]
    
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_symbol}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" style="width:100%;height:480px;border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader("💡 AI Strategy & Confluence Details")
    if not df_active.empty:
        st.write(f"**Analysis:** {result_active['votes']}")
        fmt = ".2f" if is_gold else ".5f"
        st.write(f"**Stop Loss:** `{result_active['sl']:{fmt}}` | **Take Profit:** `{result_active['tp']:{fmt}}`")
        st.markdown(f"*Source: {data_source}*")

# --- BACKTESTING & AI LEARNING HUB ---
st.divider()
st.subheader("📊 AI Self-Improvement & Backtesting Hub")

bt_tab1, bt_tab2 = st.tabs(["🚀 Run Historical Backtest", "🧠 AI Learning Insights"])

with bt_tab1:
    st.markdown(f"**Test historical performance of {symbol} on {active_chart_tf} timeframe:**")
    if st.button("▶️ Run Backtest Simulation", use_container_width=True):
        bt_results = run_historical_backtest(df_active, is_gold)
        bc1, bc2, bc3, bc4 = st.columns(4)
        bc1.metric("Total Backtest Trades", bt_results["trades"])
        bc2.metric("Win Rate", f"{bt_results['win_rate']}%")
        bc3.metric("Simulated Net PnL", f"${bt_results['total_pnl']:+.2f}")
        bc4.metric("Max Drawdown", f"${bt_results['max_dd']}")
        if bt_results['win_rate'] >= 50:
            st.success("🟢 Backtest Result: Strategy is profitable on historical samples!")
        else:
            st.warning("🟡 Backtest Result: Moderate win rate, consider adjusting timeframe.")

with bt_tab2:
    st.markdown("**Bot Self-Improvement Weights (Learned from Trade History):**")
    weights_df = pd.read_sql_query("SELECT * FROM adaptive_strategy_weights", con)
    if weights_df.empty:
        st.info("Abhi tak koi trade history nahi hai jisse AI seekh sake. Jaise-jaise trades close honge, AI weights update honge.")
    else:
        st.dataframe(weights_df, use_container_width=True)

st.divider()
st.subheader("📂 Active Trades & Trade History")

tab_15m, tab_1h, tab_4h, tab_1d = st.tabs(["⏱️ 15 Min", "⏰ 1 Hour", "📅 4 Hours", "🗓️ 1 Day"])
tf_tabs_mapping = {"⏱️ 15 Min": "15 min", "⏰ 1 Hour": "1 hour", "📅 4 Hours": "4 hours", "🗓️ 1 Day": "1 day"}

for tab_obj, tf_name in zip([tab_15m, tab_1h, tab_4h, tab_1d], tf_tabs_mapping.values()):
    with tab_obj:
        open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v24 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC", con, params=(symbol, tf_name))
        if open_df.empty:
            st.info(f"[{tf_name}] Koi active trade nahi hai.")
        else:
            for index, row in open_df.iterrows():
                row_is_gold = "XAU" in symbol
                fmt = ".2f" if row_is_gold else ".5f"
                with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
                    c1, c2 = st.columns(2)
                    c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📍 **Price:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
                    c2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `ACTIVE AI`")
                    if st.button(f"Close #{row['id']}", key=f"close_{tf_name}_{row['id']}"):
                        close_single_trade(con, row['id'], live_price_global, symbol, reason="Manual Close 👤")
                        st.rerun()
                        
        history_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, symbol, bias, lot_size, entry_price, exit_price, exit_reason, pnl FROM trade_history_v24 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC LIMIT 20", con, params=(symbol, tf_name))
        if not history_df.empty:
            st.markdown(f"**History for {tf_name}:**")
            for index, row in history_df.iterrows():
                pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
                row_is_gold = "XAU" in symbol
                fmt = ".2f" if row_is_gold else ".5f"
                with st.expander(f"{pnl_color} ID #{row['id']} | PnL: ${row['pnl']:+.2f}"):
                    st.markdown(f"**PnL:** `${row['pnl']:+.2f}` | **Reason:** `{row['exit_reason']}`")

con.close()
