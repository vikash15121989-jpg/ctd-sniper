import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER: SPRING + DRY UP SCANNER START ===")

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("creek_scanner")

ws_watchlist = sh.worksheet("Watchlist")
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, '%d/%m/%Y').strftime('%Y-%m-%d')
print(f"Backtest Date: {end_date}")

stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start="2023-01-01", end=end_date, interval="1d", progress=False, auto_adjust=True)
        if len(df) < 200: continue

        df['Vol_50D'] = df['Volume'].rolling(50).mean()
        if df['Vol_50D'].iloc[-1] < 100000: continue

        df['EMA200'] = df['Close'].ewm(span=200).mean()

        for j in range(-60, -5):
            candle = df.iloc[j]
            support = min(candle['EMA200'], df.iloc[j-20:j]['Low'].min())
            is_spring = candle['Low'] < support * 0.99 and candle['Close'] > support

            if is_spring:
                spring_date = df.index[j].date()
                post_spring = df.iloc[j+1:]
                if len(post_spring) < 5: continue

                last_swing_high = post_spring['High'].max()
                current_close = df['Close'].iloc[-1]
                current_vol_avg = post_spring['Volume'].tail(5).mean()
                spring_vol = candle['Volume']
                spring_vol_50d = df['Vol_50D'].iloc[j]

                cmp_below_swing = current_close < last_swing_high * 1.01 # 1% buffer
                is_dry = current_vol_avg < spring_vol * 0.4
                current_range = (last_swing_high - candle['Low']) / candle['Low'] * 100
                is_tight = current_range < 15

                status = "READY" if cmp_below_swing else "BREAKOUT"

                if is_dry and is_tight:
                    signals.append([
                        stock, str(spring_date), end_date,
                        round(last_swing_high, 2), round(candle['Low'] * 0.98, 2),
                        round(last_swing_high * 1.08, 2), round(current_close, 2),
                        round(current_range, 1), int(current_vol_avg / spring_vol_50d * 100),
                        status, f"Spring:{spring_date} | Dry:{is_dry} | Tight:{is_tight}"
                    ])
                    print(f"[PASS] ✅ {stock}: {status}")
                    break

    except Exception as e: print(f"[ERROR] {stock}: {e}")

ws_live = sh.worksheet("LiveSignals")
ws_live.clear()
header = ['Stock','Spring_Date','Scan_Date','Entry_BO','SL','TGT1','CMP','Range%','Dry%','Status','Reason']
ws_live.update('A1', [header] + signals)
print(f"\n=== DONE === {len(signals)} signals mile.")
