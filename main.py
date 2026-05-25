import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER: 20 DAYS BACK SCANNER START ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. DATE READ KARO A1 SE - YE END DATE HOGI
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Scan End Date: {end_date_str} | Day: {end_date.strftime('%A')}")
print(f"Scanning Last 20 Trading Days...")

# 3. STOCK LIST READ KARO A COLUMN SE
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

all_history = [] # Sab stocks ka 20 din ka data
ready_signals = [] # Sirf latest date ke READY

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        # 4. DATA DOWNLOAD - 6 month ka taaki 20 din + 90 din spring ke liye mile
        start_date = end_date - timedelta(days=250) # ~6 month buffer
        df = yf.download(f"{stock}.NS", start=start_date.strftime('%Y-%m-%d'), end=(end_date + timedelta(days=1)).strftime('%Y-%m-%d'), interval="1d", progress=False, auto_adjust=True)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 120:
            print(f" ❌ {stock}: Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df = df.dropna() # NaN rows hata do

        # 5. LAST 20 TRADING DAYS NIKALO
        df_last_20 = df.iloc[-20:].copy() # Last 20 candles

        if len(df_last_20) < 20:
            print(f" ❌ {stock}: 20 trading days nahi mile")
            continue

        # 6. HAR DIN KE LIYE CTD CHECK KARO
        for j in range(len(df_last_20)):
            bo_candle = df_last_20.iloc[j]
            bo_date = bo_candle.name
            # Us din tak ka past data
            df_past = df.loc[:bo_date].iloc[:-1]

            if len(df_past) < 90:
                continue # Spring nikalne ke liye 90 din chahiye

            # SPRING DHOONDO = Pichle 90 din ka Lowest Low
            df_90d = df_past.iloc[-90:].copy()
            spring_low = df_90d['Low'].min()
            spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
            spring_idx = df_90d.index.get_loc(spring_candle.name)

            # CREEK DHOONDO = Spring se pehle ka Highest High
            df_before_spring = df_90d.iloc[:spring_idx+1]
            if df_before_spring.empty:
                continue
            creek_high = df_before_spring['High'].max()

            # CTD CONDITIONS
            vol_condition = bo_candle['Volume'] < bo_candle['Vol_50'] * 1.2
            breakout = bo_candle['Close'] > creek_high
            status = 'READY' if (vol_condition and breakout) else 'NA'

            # HISTORY KE LIYE STORE KARO
            all_history.append({
                'Date': bo_date.strftime('%d/%m/%Y'),
                'Stock': stock,
                'Status': status,
                'SpringDate': spring_candle.name.strftime('%d/%m/%Y'),
                'SpringLow': round(spring_low, 2),
                'CreekHigh': round(creek_high, 2),
                'Close': round(bo_candle['Close'], 2),
                'Volume': int(bo_candle['Volume']),
                'Vol_50': int(bo_candle['Vol_50'])
            })

            # AGAR YE LATEST DATE HAI AUR READY HAI TO SEPARATE LIST ME
            if j == len(df_last_20) - 1 and status == 'READY':
                ready_signals.append({
                    'Stock': stock,
                    'Status': 'READY',
                    'Date': bo_date.strftime('%d/%m/%Y'),
                    'SpringDate': spring_candle.name.strftime('%d/%m/%Y'),
                    'SpringLow': round(spring_low, 2),
                    'CreekHigh': round(creek_high, 2),
                    'Close': round(bo_candle['Close'], 2),
                    'Volume': int(bo_candle['Volume']),
                    'Vol_50': int(bo_candle['Vol_50'])
                })
                print(f"[PASS] ✅ {stock}: READY on {bo_date.date()}")

        print(f" ✅ {stock}: 20 days scanned")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. HISTORY SHEET UPDATE - APPEND MODE
try:
    ws_history = sh.worksheet("History")
    if all_history:
        df_hist = pd.DataFrame(all_history)
        # Duplicate remove karo - same Stock + Date pehle se ho to
        existing = ws_history.get_all_records()
        if existing:
            df_existing = pd.DataFrame(existing)
            df_hist['Key'] = df_hist['Date'] + '_' + df_hist['Stock']
            df_existing['Key'] = df_existing['Date'] + '_' + df_existing['Stock']
            df_hist = df_hist[~df_hist['Key'].isin(df_existing['Key'])]
            df_hist = df_hist.drop('Key', axis=1)

        if not df_hist.empty:
            if len(ws_history.get_all_values()) <= 1:
                ws_history.update([df_hist.columns.values.tolist()] + df_hist.values.tolist())
            else:
                ws_history.append_rows(df_hist.values.tolist())
            print(f"\n=== HISTORY UPDATED: {len(df_hist)} new rows added ===")
        else:
            print("\n=== HISTORY: No new data to add ===")
except Exception as e:
    print(f"History Sheet Error: {e}")

# 8. LIVE SIGNALS - SIRF LATEST DATE KE READY
try:
    ws_output = sh.worksheet("LiveSignals")
    ws_output.clear()
    if ready_signals:
        df_out = pd.DataFrame(ready_signals)
        ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"\n=== SCAN COMPLETE: {len(ready_signals)} READY SIGNALS ON {end_date_str} ===")
    else:
        ws_output.update([["No READY signals found on latest date"]])
        print(f"\n=== SCAN COMPLETE: 0 READY SIGNALS ON {end_date_str} ===")
except Exception as e:
    print(f"Sheet Update Error: {e}")
