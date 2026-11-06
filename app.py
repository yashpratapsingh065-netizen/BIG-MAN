def fetch_exact_market_data(asset, tf_name):
    sym = ASSETS[asset]["td"]
    interval_td = TFS[tf_name][2]
    
    # Direct Live Price Fetching via Twelve Data API
    if API_KEY:
        try:
            # Fetch real-time live price ticker directly
            q = HTTP.get("https://api.twelvedata.com/price", params={"symbol": sym, "apikey": API_KEY}, timeout=6)
            live_px = 0.0
            if q.status_code == 200:
                qj = q.json()
                if "price" in qj:
                    live_px = float(qj["price"])
            
            # Fetch time series candles for multi-timeframe & indicators
            df = cached_twelve_data(sym, interval_td, API_KEY)
            if not df.empty:
                if live_px <= 0:
                    live_px = float(df.iloc[-1]["close"])
                return df, live_px, f"Twelve Data Live Verified ({sym})"
        except Exception:
            pass

    # Fallback to MT5 if local terminal is active
    if MT5_OK:
        try:
            mt5_init()
            mt_sym = ASSETS[asset]["mt5"]
            mt5.symbol_select(mt_sym, True)
            tick = mt5.symbol_info_tick(mt_sym)
            tf_attr = TFS[tf_name][1]
            raw = mt5.copy_rates_from_pos(mt_sym, getattr(mt5, tf_attr), 0, 120)
            if raw is not None and len(raw) > 30 and tick is not None:
                d = pd.DataFrame(raw)
                d["datetime"] = pd.to_datetime(d.time, unit="s", utc=True)
                d = d.rename(columns={"tick_volume": "volume"}).set_index("datetime").sort_index()
                d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
                d["ema50"] = d.close.ewm(span=50, adjust=False).mean()
                tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
                d["atr"] = tr.rolling(14).mean()
                return d, float(tick.bid), f"MT5 Broker Live Feed ({mt_sym})"
        except Exception:
            pass

    # Absolute Last Resort Live Fetch via Public Yahoo Data to prevent 0.0 error
    try:
        yf_ticker = "GC=F" if "Gold" in asset else ("EURUSD=X" if "EUR" in asset else "GBPUSD=X")
        tk = yf.Ticker(yf_ticker)
        df_yf = tk.history(period="5d", interval="15m")
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
            df_yf.index = pd.to_datetime(df_yf.index, utc=True)
            df_yf = df_yf.dropna(subset=["open", "high", "low", "close"]).sort_index()
            df_yf["ema20"] = df_yf.close.ewm(span=20, adjust=False).mean()
            df_yf["ema50"] = df_yf.close.ewm(span=50, adjust=False).mean()
            tr = pd.concat([df_yf.high - df_yf.low, (df_yf.high - df_yf.close.shift()).abs(), (df_yf.low - df_yf.close.shift()).abs()], axis=1).max(axis=1)
            df_yf["atr"] = tr.rolling(14).mean()
            live_px = float(df_yf.iloc[-1]["close"])
            return df_yf, live_px, f"Yahoo Live Stream ({yf_ticker})"
    except Exception:
        pass

    return pd.DataFrame(), 0.0, "Feed Error"
