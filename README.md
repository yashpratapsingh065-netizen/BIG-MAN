# BIG MAN Trading Lab v1.0

Beginner-friendly Streamlit prototype for Android browser use via a cloud coding workspace.

## Run
1. Upload/extract this folder in a Python workspace that supports Streamlit.
2. Install dependencies from `requirements.txt`.
3. Run: `streamlit run app.py`

## Included
- Strategy library: `strategies.json` (8 draft strategy records)
- Synthetic sample chart and illustrative MA signal
- Local SQLite paper-trade journal (`big_man.db` is created automatically)
- Manual virtual trade entry/exit
- Risk/target illustration

## Important limitations
- Chart prices are synthetic and NOT live Forex data.
- The analyzer's simple MA signal is only a software demonstration, not the eight-strategy engine.
- P/L in the paper journal is simplified price-units × units, not account-currency P/L.
- No broker/API connection, credentials, or real order execution.
- Strategy win rates are unknown and must be tested using reliable historical data, realistic transaction costs, and out-of-sample evaluation.
