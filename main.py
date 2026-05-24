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

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        if len(df) < 60: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df['Body'] = abs(df['Close'] - df['Open'])
        df['Range'] = df['High'] - df['Low']
        df['Range'] = df['Range'].replace(0, 0.01)
        df['BodyRatio'] = df['Body'] / df['Range']
        df['IsGreen'] = df['Close'] > df['Open']

        # FIX 1: BO candle hata ke creek nikalo - No lookahead bug
        df_past = df.iloc[:-1] # Last candle = BO candle, use hatao
        if len(df_past) < 60: continue
        df_sig = df_past.iloc[-60:].copy() # Pichle 60 din ka range

        creek_high = df_sig['High'].max() # Ab ye BO candle se pehle ka max hai
        spring_low = df_sig['Low'].min()

        # BO Candle = Backtest date wali candle
        bo_candle = df.iloc[-1]
        last_close = bo_candle['Close']
        last_vol = bo_candle['Volume']
        last_vol_50 = bo_candle['Vol_50']

        # Spring Candle Logic
        spring_candle = df_sig.loc[df_sig['Low'] == spring_low].iloc[-1]
        is_spring = spring_candle['IsGreen'] # Bas green close chahiye, body ratio hata diya

        # FIX 2: Volume Dry Logic Dheela kiya - 90% tak allow
        vol_condition = last_vol < last_vol_50 * 0.9

        # FIX 3: EMA200 Hata Diya - CTD me zarurat nahi

        # FIX 4: BO Logic - 0.5% bhi chalega, 1% zaruri nahi
        breakout = last_close > creek_high * 1.005

        # FIX 5: Dryup condition hata di - Real CTD me zaruri nahi
        if is_spring and vol_condition and breakout:
            signals.append({
                'Stock': stock,
                'Status': 'READY',
                'SpringLow': round(spring_low, 2),
                'CreekHigh': round(creek_high, 2),
                'Close': round(last_close, 2),
                'Volume': int(last_vol),
                'Vol_50': int(last_vol_50)
            })
            print(f"[PASS] ✅ {stock}: READY")
        else:
            reason = []
            if not is_spring: reason.append("Spring green nahi")
            if not vol_condition: reason.append("Volume dry nahi")
            if not breakout: reason.append(f"BO nahi: {last_close:.2f} < {round(creek_high*1.005,2)}")
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
