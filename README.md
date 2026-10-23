BIG MAN2 — Price Action + TradingView chart
Files
app.py: Streamlit app with an embedded TradingView chart, basic price-action pattern checks, and SQLite analysis log.
requirements.txt: Python dependencies.
Deploy to Streamlit Community Cloud
Put app.py and requirements.txt in your GitHub repository.
In Streamlit Cloud, deploy/redeploy the app from that repository.
Optional: add a Twelve Data key in App settings → Secrets:
TWELVE_DATA_API_KEY = "your_key_here"
Restart the app.
Important limitation
TradingView is embedded for chart display; the app does not pull candle data from a TradingView API. OHLC data for analysis comes from Twelve Data when configured, otherwise Yahoo Finance fallback. To use TradingView alert webhooks as a signal source, a separate public webhook endpoint and a TradingView alert configuration are required.
The SQLite database is local to the app runtime. Streamlit Cloud local storage is not guaranteed to persist across restarts/redeploys; use a hosted persistent database for long-term history.
This is an educational/demo analysis tool. Signals are not guaranteed and the app does not place broker orders.