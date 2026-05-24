import yfinance as yf
import pandas as pd
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# TERA WATCHLIST SHEET KA LINK - YEHI SE STOCK UTHEGA
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vS7zjzUB789rZP6lIdnpC5o-kJqMvibj4wdLyXL0IMrbQjBJQZChwySK-efW9ERj2T7yyTNbVxHhQeU/pub?gid=0&single=true&output=csv"

# 1. Seedha Google Sheet se Watchlist uthao
df_watchlist = pd.read_csv(SHEET_CSV_URL, header=None)
stocks = df_watchlist[0].dropna().astype(str).str.strip().str.upper().tolist()
# Header 'COFORGE' hai to skip mat karo, 'A' hota to karte
if stocks[0] == 'A': stocks = stocks[1:]
print(f"Watchlist Sheet se {len(stocks)} stocks mile: {stocks[:3]}...")

signals = []
for i, stock in enumerate(stocks):
    if i % 10 == 0: print(f"Scanning {i}/{len(stocks)}... {stock}")
    try:
        df = yf.download(f"{stock}.NS", period="6mo", interval="1d", progress=False, auto_adjust=True)
        if len(df) < 100: continue

        df['EMA200'] = df['Close'].ewm(span=200).mean()
        df['Vol_50D'] = df['Volume'].rolling(50).mean()

        for j in range(-30, -5):
            candle = df.iloc[j]
            support = min(candle['EMA200'], df.iloc[j-10:j]['Low'].min())

            # Spring Condition
            if candle['Low'] < support * 0.99 and candle['Close'] > support:
                post = df.iloc[j:]
                rng_high, rng_low = post['High'].max(), post['Low'].min()

                # Tight Range + Absorption Check
                if (rng_high - rng_low) / rng_low * 100 < 12:
                    if candle['Volume'] > candle['Vol_50D'] * 1.2: # Spring pe volume spike
                        if post['Volume'].tail(10).mean() < candle['Vol_50D'] * 0.6: # Ab dead
                            signals.append({
                                'Stock': stock,
                                'Entry': round(rng_high, 2),
                                'SL': round(support * 0.98, 2),
                                'TGT1': round(rng_high * 1.10, 2),
                                'TGT2': round(rng_high * 1.20, 2),
                                'Time': datetime.now().strftime("%d-%m %H:%M")
                            })
                            break
    except Exception as e: continue

# 2. Output CSV - LiveSignals me jayega
df_out = pd.DataFrame(signals)
df_out.to_csv("spring_breakout_signals.csv", index=False)
print(f"\nTotal {len(df_out)} Spring signals mile. CSV ready.")
if not df_out.empty: print(df_out.to_string(index=False))
