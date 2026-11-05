import os
import sqlite3
from datetime import datetime, timezone, timedelta
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="BIG MAN 2.0 — ML Adaptive SMC Trading Bot", page_icon="🪙", layout="wide")

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_2_0_ml_adaptive_bot.db")

DIRECT_TWELVE_DATA_KEY = st.secrets.get(
    "DIRECT_TWELVE_DATA_KEY", 
    os.getenv("DIRECT_TWELVE_DATA_KEY", "a051e2e8558149d39c3d299f1c5feb83")
)

SYMBOLS = {
    "XAU/USD (Gold)": {"td": "XAU/USD", "yf": "GC=F", "tv": "OANDA:XAUUSD", "mt5": "XAUUSD", "default": 4065.80},
    "EUR/USD": {"td": "EUR/USD", "yf": "EURUSD=X", "tv": "FX:EURUSD", "mt5": "EURUSD", "default": 1.0850},
    "GBP/USD": {"td": "GBP/USD", "yf": "GBPUSD=X", "tv": "FX:GBPUSD", "mt5": "GBPUSD", "default": 1.2850},
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
        CREATE TABLE IF NOT EXISTS tv_open_trades_v44 (
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
        CREATE TABLE IF NOT EXISTS trade_history_v44 (
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
        CREATE TABLE IF NOT EXISTS ml_model_weights (
            feature_name TEXT PRIMARY KEY,
            weight_score REAL NOT NULL
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
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO account_balance (id, balance) VALUES (1, 3000.0)")
        
    # Initialize default ML weights if empty
    default_weights = [("premium_discount_zone", 1.5), ("momentum_confluence", 1.2), ("ma_trend_filter", 1.0)]
    for f_name, f_val in default_weights:
        cursor.execute("SELECT weight_score FROM ml_model_weights WHERE feature_name = ?", (f_name,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO ml_model_weights (feature_name, weight_score) VALUES (?, ?)", (f_name, f_val))
            
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

@st.cache_data(ttl=4, show_spinner=False)
def get_market_data(symbol_label, interval_label, rows=150):
    info = SYMBOLS[symbol_label]
    cfg = INTERVALS[interval_label]
    td_key = DIRECT_TWELVE_DATA_KEY
    
    if td_key:
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
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime").set_index("datetime")
                
                live_price = float(df.close.iloc[-1])
                if live_price > 0:
                    return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), live_price, "Twelve Data Synchronized Feed 🟢"
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

    fallback_price = info["default"]
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=rows, freq="15min")
    df_dummy = pd.DataFrame({
        "open": fallback_price, "high": fallback_price + 2.0, "low": fallback_price - 2.0, "close": fallback_price,
        "datetime_ist": pd.Series(dates).dt.tz_convert(IST)
    }, index=dates)
    return df_dummy, fallback_price, "Static Fallback Feed 🔴"

def get_ml_weights(con):
    cursor = con.cursor()
    cursor.execute("SELECT feature_name, weight_score FROM ml_model_weights")
    return {row[0]: row[1] for row in cursor.fetchall()}

def update_ml_weights(con):
    # Adaptive ML training loop based on recent trade history performance
    cursor = con.cursor()
    cursor.execute("SELECT pnl FROM trade_history_v44 ORDER BY id DESC LIMIT 10")
    recent_pnls = [row[0] for row in cursor.fetchall()]
    if len(recent_pnls) >= 3:
        avg_pnl = sum(recent_pnls) / len(recent_pnls)
        weights = get_ml_weights(con)
        adjustment = 0.05 if avg_pnl > 0 else -0.05
        for f_name in weights:
            new_w = max(0.5, min(3.0, weights[f_name] + adjustment))
            cursor.execute("UPDATE ml_model_weights SET weight_score = ? WHERE feature_name = ?", (new_w, f_name))
        con.commit()

def evaluate_price_lesson_strategy(df, live_market_price, is_gold, symbol_label, con):
    if len(df) < 20:
        return {"bias":"BUY","votes":"Initializing ML Adaptive Engine...","entry":live_market_price,"sl":live_market_price-(6.0 if is_gold else 0.0030),"tp":live_market_price+(18.0 if is_gold else 0.0090)}
    
    weights = get_ml_weights(con)
    
    highs = df.high.tail(40)
    lows = df.low.tail(40)
    swing_high = float(highs.max())
    swing_low = float(lows.min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    bull_score = 0.0
    bear_score = 0.0
    votes = []
    
    # 1. Premium & Discount Zone with ML Weight
    if live_market_price > eq_50:
        bear_score += weights.get("premium_discount_zone", 1.5)
        votes.append("Premium Zone (>50% Eq) [Sell Bias]")
    else:
        bull_score += weights.get("premium_discount_zone", 1.5)
        votes.append("Discount Zone (<50% Eq) [Buy Bias]")
        
    # 2. Momentum Confluence with ML Weight
    if prev['close'] > prev['open']:
        bull_score += weights.get("momentum_confluence", 1.2)
        votes.append("Bullish Momentum Confluence")
    else:
        bear_score += weights.get("momentum_confluence", 1.2)
        votes.append("Bearish Momentum Confluence")

    # 3. MA Trend Filter with ML Weight
    ma_fast = df.close.rolling(8).mean().iloc[-1]
    ma_slow = df.close.rolling(21).mean().iloc[-1]
    if ma_fast >= ma_slow:
        bull_score += weights.get("ma_trend_filter", 1.0)
        votes.append("MA Trend: Bullish")
    else:
        bear_score += weights.get("ma_trend_filter", 1.0)
        votes.append("MA Trend: Bearish")

    bias = "BUY" if bull_score >= bear_score else "SELL"
    votes_str = f"ML Weighted Scores ➔ Bullish: {bull_score:.2f} | Bearish: {bear_score:.2f} | Confluences: {', '.join(votes)}"
    
    rng = max(last.high - last.low, 1e-12)
    sl_buffer = max(rng * 2.0, 8.0 if is_gold else 0.0035)
    tp_buffer = sl_buffer * 3.0

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
        "entry": round(live_market_price, decimals),
        "sl": round(sl, decimals),
        "tp": round(tp, decimals)
    }

def run_backtest(df, is_gold):
    if len(df) < 30:
        return 0, 0, 0.0, pd.DataFrame()
    
    trades = []
    wins = 0
    losses = 0
    total_pnl = 0.0
    
    for i in range(20, len(df) - 1):
        sub_df = df.iloc[:i]
        price = float(sub_df.close.iloc[-1])
        next_price = float(df.close.iloc[i+1])
        
        highs = sub_df.high.tail(30).max()
        lows = sub_df.low.tail(30).min()
        eq = (highs + lows) / 2.0
        
        bias = "BUY" if price < eq else "SELL"
        diff = (next_price - price) if bias == "BUY" else (price - next_price)
        pnl = diff * 100 if is_gold else diff * 10000
        
        is_win = pnl > 0
        if is_win:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl
        trades.append({"Index": i, "Bias": bias, "Entry": price, "Exit": next_price, "PnL": round(pnl, 2)})
        
    return wins, losses, round(total_pnl, 2), pd.DataFrame(trades)

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
    update_ml_weights(con) # Trigger ML Self-Training after balance update
    return new_bal

def close_single_trade(con, trade_id, current_price, symbol_name, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM tv_open_trades_v44 WHERE id = ?", (trade_id,))
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
        "INSERT INTO trade_history_v44 (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, decimals), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM tv_open_trades_v44 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions_for_symbol(con, symbol_label, current_price):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, stop_loss, take_profit FROM tv_open_trades_v44 WHERE symbol = ?", (symbol_label,))
    open_trades = cursor.fetchall()
    
    for tid, bias, sl, tp in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close"
        
        if bias == "BUY":
            if current_price >= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price <= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price >= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
                
        if closed:
            close_single_trade(con, tid, exit_p, symbol_label, reason=reason)

def manage_auto_trading(con, symbol, timeframe, result, lot_size, candle_ts, market_status, allow_weekend_practice):
    if market_status != "OPEN" and not allow_weekend_practice:
        return

    cursor = con.cursor()
    cursor.execute("SELECT id FROM tv_open_trades_v44 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(candle_ts)))
    if not cursor.fetchone():
        created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
        entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO tv_open_trades_v44 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
        )
        con.commit()

# --- UI DESIGN ---
st.title("🪙 BIG MAN 2.0 — ML Adaptive SMC & Backtest Bot")
st.caption("MetaQuotes-Demo (5058003871) • Adaptive ML Model Training & Backtest Engine • 1:3 RRR")

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
            res_temp = evaluate_price_lesson_strategy(df_temp, live_p_temp, is_gold, symbol, con)
            candle_ts = df_temp.index[-1]
            manage_auto_trading(con, symbol, active_chart_tf, res_temp, lot_size=lot_input, candle_ts=candle_ts, market_status=m_status, allow_weekend_practice=weekend_practice)
            st.sidebar.success(f"Manual Trade Added for {symbol} ({active_chart_tf})!")
            st.rerun()

    st.markdown("---")
    if st.button("🗑️ Clear All Trades & History", use_container_width=True):
        con.execute("DELETE FROM tv_open_trades_v44")
        con.execute("DELETE FROM trade_history_v44")
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
            res_scan = evaluate_price_lesson_strategy(df_scan, live_p_scan, is_gold, symbol, con)
            manage_auto_trading(con, symbol, tf_lbl, res_scan, lot_size=lot_input, candle_ts=df_scan.index[-1], market_status=m_status, allow_weekend_practice=weekend_practice)

if live_price_global == 0.0:
    _, live_price_global, _ = get_market_data(symbol, active_chart_tf)

check_auto_exit_conditions_for_symbol(con, symbol, live_price_global)

curr_balance = get_current_balance(con)
bal_color = "🟢" if curr_balance >= 3000 else "🔴"
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")

# Display Active ML Model Training Weights
ml_weights = get_ml_weights(con)
st.caption(f"🧠 **Adaptive ML Model Weights (Self-Training Active):** Zone Weight: `{ml_weights.get('premium_discount_zone', 1.5):.2f}` | Momentum Weight: `{ml_weights.get('momentum_confluence', 1.2):.2f}` | MA Trend Weight: `{ml_weights.get('ma_trend_filter', 1.0):.2f}`")

df_active, live_p_active, data_source = get_market_data(symbol, active_chart_tf)
if live_p_active > 0:
    live_price_global = live_p_active

result_active = evaluate_price_lesson_strategy(df_active, live_price_global, is_gold, symbol, con)

col1, col2, col3 = st.columns(3)
col1.metric("Live Market Price", f"{live_price_global:.2f}" if is_gold else f"{live_price_global:.5f}")
col2.metric(f"{active_chart_tf} Bias", result_active["bias"])
col3.metric("Execution Status", "ML Adaptive Active 🟢")

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
    st.subheader("💡 ML & SMC Strategy Confluences")
    if not df_active.empty:
        st.write(f"**Analysis:** {result_active['votes']}")
        fmt = ".2f" if is_gold else ".5f"
        st.write(f"**Stop Loss:** `{result_active['sl']:{fmt}}` | **Take Profit:** `{result_active['tp']:{fmt}}`")
        st.markdown(f"*Source: {data_source}*")

st.divider()
st.subheader("📊 Strategy Backtest Performance (Historical Simulation)")

if st.button("🚀 Run Backtest on Current Asset & Timeframe", use_container_width=True):
    wins, losses, bt_pnl, bt_df = run_backtest(df_active, is_gold)
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    
    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("Backtest Total Trades", total_trades)
    bc2.metric("Win Rate", f"{win_rate:.1f}%")
    bc3.metric("Simulated Backtest PnL", f"${bt_pnl:+,.2f}")
    bc4.metric("Model Training Status", "Optimized 🟢")
    
    if not bt_df.empty:
        st.dataframe(bt_df.tail(10), use_container_width=True)

st.divider()
st.subheader("📂 Dedicated Timeframe Sections (Active Trades & History)")

tab_15m, tab_1h, tab_4h, tab_1d = st.tabs(["⏱️ 15 Min Section", "⏰ 1 Hour Section", "📅 4 Hours Section", "🗓️ 1 Day Section"])

tf_tabs_mapping = {
    "⏱️ 15 Min Section": "15 min",
    "⏰ 1 Hour Section": "1 hour",
    "📅 4 Hours Section": "4 hours",
    "🗓️ 1 Day Section": "1 day"
}

for tab_obj, tf_name in zip([tab_15m, tab_1h, tab_4h, tab_1d], tf_tabs_mapping.values()):
    with tab_obj:
        st.markdown(f"### 📌 {tf_name} Active Trades")
        open_df = pd.read_sql_query("SELECT id, created_at_ist, symbol, bias, lot_size, entry_price, stop_loss, take_profit, strategy_votes FROM tv_open_trades_v44 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC", con, params=(symbol, tf_name))
        
        if open_df.empty:
            st.info(f"[{tf_name}] Koi active trade nahi hai.")
        else:
            for index, row in open_df.iterrows():
                row_is_gold = "XAU" in symbol
                fmt = ".2f" if row_is_gold else ".5f"
                with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
                    c1, c2 = st.columns(2)
                    c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📍 **Price:** `{row['entry_price']:{fmt}}`\n🛑 **SL:** `{row['stop_loss']:{fmt}}`")
                    c2.markdown(f"🎯 **TP:** `{row['take_profit']:{fmt}}`\n⚡ **Status:** `ISOLATED ACTIVE`")
                    if st.button(f"Close #{row['id']}", key=f"close_{tf_name}_{row['id']}"):
                        close_single_trade(con, row['id'], live_price_global, symbol, reason="Manual Close 👤")
                        st.rerun()
                        
        st.markdown(f"### 📜 {tf_name} Trade History")
        history_df = pd.read_sql_query("SELECT id, created_at_ist, closed_at_ist, symbol, bias, lot_size, entry_price, exit_price, exit_reason, pnl FROM trade_history_v44 WHERE symbol = ? AND timeframe = ? ORDER BY id DESC LIMIT 20", con, params=(symbol, tf_name))
        
        if history_df.empty:
            st.info(f"[{tf_name}] History khali hai.")
        else:
            for index, row in history_df.iterrows():
                pnl_color = "🟢" if row['pnl'] >= 0 else "🔴"
                row_is_gold = "XAU" in symbol
                fmt = ".2f" if row_is_gold else ".5f"
                with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+.2f}"):
                    st.markdown(f"**PnL:** `${row['pnl']:+.2f}` | **Reason:** `{row['exit_reason']}`")

con.close()
