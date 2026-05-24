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
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y").strftime('%Y-%m-%d')
print(f"Backtest Date: {end_date}")

stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start="2023-01-01", end=end_date, interval="1d", progress=False, auto_adjust=True)

        # FIX: yfinance MultiIndex columns hatao
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        if len(df) < 200: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
        df['Body'] = abs(df['Close'] - df['Open'])
        df['Range'] = df['High'] - df['Low']
        df['Range'] = df['Range'].replace(0, 0.01)
        df['BodyRatio'] = df['Body'] / df['Range']
        df['IsGreen'] = df['Close'] > df['Open']

        df_sig = df.iloc[-60:].copy()

        # CTD Logic
        base_condition = df_sig.iloc[-1]['Close'] > df_sig.iloc[-1]['EMA200']
        vol_condition = df_sig.iloc[-1]['Volume'] < df_sig.iloc[-1]['Vol_50'] * 0.5

        creek_high = df_sig['High'].max()
        last_close = df_sig.iloc[-1]['Close']
        spring_low = df_sig['Low'].min()

        spring_candle = df_sig.loc[df_sig['Low'] == spring_low].iloc[-1]
        is_spring = spring_candle['IsGreen'] and spring_candle['BodyRatio'] < 0.3

        dryup_condition = False
        if is_spring:
            spring_idx = df_sig.index.get_loc(spring_candle.name)
            if spring_idx < len(df_sig) - 2:
                test_candles = df_sig.iloc[spring_idx+1:]
                dryup_condition = (test_candles['Volume'] < test_candles['Vol_50']).all()

        breakout = last_close > creek_high * 1.01

        if base_condition and vol_condition and is_spring and dryup_condition and breakout:
            signals.append({
                'Stock': stock,
                'Status': 'READY',
                'SpringLow': round(spring_low, 2),
                'CreekHigh': round(creek_high, 2),
                'Close': round(last_close, 2),
                'Volume': int(df_sig.iloc[-1]['Volume']),
                'Vol_50': int(df_sig.iloc[-1]['Vol_50'])
            })
            print(f"[PASS] ✅ {stock}: READY")
        else:
            reason = []
            if not base_condition: reason.append("EMA200 ke neeche")
            if not vol_condition: reason.append("Volume dry nahi")
            if not is_spring: reason.append("Spring nahi bana")
            if not dryup_condition: reason.append("Test me volume aaya")
            if not breakout: reason.append(f"BO nahi: {last_close} < {round(creek_high*1.01,2)}")
            print(f"[FAIL] ❌ {stock}: {', '.join(reason)}")

    except Exception as e:
        print(f"Error: {stock}: {e}")
        continue

# Output to Google Sheet
try:
    ws_output = sh.worksheet("LiveSignals")
    ws_output.clear()
    if signals:
        df_out = pd.DataFrame(signals)
        ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"\n=== SCAN COMPLETE: {len(signals)} SIGNALS FOUND ===")
    else:
        ws_output.update([["No READY signals found on this date"]])
        print("\n=== SCAN COMPLETE: 0 SIGNALS FOUND ===")
except Exception as e:
    print(f"Sheet Update Error: {e}")
