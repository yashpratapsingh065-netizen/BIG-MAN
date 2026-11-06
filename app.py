import time

@st.cache_data(ttl=60, show_spinner=False)
def cached_twelve_data(sym, interval, apikey):
    # Added a polite delay to respect Twelve Data free-tier rate limits
    time.sleep(1.2)
    url = "https://api.twelvedata.com"
    p = {"symbol": sym, "interval": interval, "outputsize": 250, "timezone": "UTC", "apikey": apikey}
    r = HTTP.get(url + "/time_series", params=p, timeout=15)
    r.raise_for_status()
    j = r.json()
    if "values" not in j:
        raise RuntimeError("Twelve Data candles: " + str(j.get("message", j)))
    
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
