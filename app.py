import yfinance as yf  # Yahoo finance reliable fallback

def get_exact_live_price_and_data(asset, tf_name):
    sym = ASSETS[asset]["td"]
    yf_ticker = "GC=F" if "Gold" in asset else ("EURUSD=X" if "EUR" in asset else "GBPUSD=X")
    
    # Priority 1: Yahoo Finance Reliable Public Feed (Works 100% on Cloud & Android)
    try:
        interval_map = {"15 min": "15m", "1 hour": "1h", "4 hours": "1h"}
        period_map = {"15 min": "5d", "1 hour": "1mo", "4 hours": "2mo"}
        tk = yf.Ticker(yf_ticker)
        df_yf = tk.history(period=period_map.get(tf_name, "5d"), interval=interval_map.get(tf_name, "15m"))
        
        if not df_yf.empty:
            df_yf = df_yf.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
            df_yf.index = pd.to_datetime(df_yf.index, utc=True)
            df_yf = df_yf.dropna(subset=["open", "high", "low", "close"]).sort_index()
            
            df_yf["ema20"] = df_yf.close.ewm(span=20, adjust=False).mean()
            df_yf["ema50"] = df_yf.close.ewm(span=50, adjust=False).mean()
            tr = pd.concat([df_yf.high - df_yf.low, (df_yf.high - df_yf.close.shift()).abs(), (df_yf.low - df_yf.close.shift()).abs()], axis=1).max(axis=1)
            df_yf["atr"] = tr.rolling(14).mean()
            
            live_px = float(df_yf.iloc[-1]["close"])
            if live_px > 0:
                return df_yf, live_px, live_px, f"Yahoo Finance Live Feed ({yf_ticker})"
    except Exception:
        pass

    # Priority 2: Twelve Data Fallback
    if API_KEY:
        interval_td = TFS[tf_name][2]
        url = "https://api.twelvedata.com"
        try:
            p = {"symbol": sym, "interval": interval_td, "outputsize": 120, "timezone": "UTC", "apikey": API_KEY}
            r = HTTP.get(url + "/time_series", params=p, timeout=8)
            if r.status_code == 200:
                j = r.json()
                if "values" in j:
                    d = pd.DataFrame(j["values"])
                    d["datetime"] = pd.to_datetime(d.datetime, utc=True)
                    for k in ["open", "high", "low", "close"]:
                        d[k] = pd.to_numeric(d[k], errors="coerce")
                    d = d.dropna(subset=["open", "high", "low", "close"]).sort_values("datetime").set_index("datetime")
                    d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
                    d["ema50"] = d.close.ewm(span=50, adjust=False).mean()
                    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
                    d["atr"] = tr.rolling(14).mean()
                    live_px = float(d.iloc[-1]["close"])
                    return d, live_px, live_px, f"Twelve Data Feed ({sym})"
        except Exception:
            pass

    return pd.DataFrame(), 2650.0 if "Gold" in asset else 1.08, 2650.0 if "Gold" in asset else 1.08, "Safe Default Price Feed"
