import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER: 20 DAYS FINAL SCANNER ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. END DATE = A1 SE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Scan End Date: {end_date_str} | Scanning Last 20 Trading Days...")

# 3. STOCK LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

ctd_found = [] # Sirf proper CTD wale

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        # 4. 6 MONTH DATA DOWNLOAD
        start_date = end_date - timedelta(days=250)
        df = yf.download(f"{stock}.NS", start=start_date.strftime('%Y-%m-%d'), end=(end_date + timedelta(days=1)).strftime('%Y-%m-%d'), interval="1d", progress=False, auto_adjust=True)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 120:
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df = df.dropna()

        # 5. LAST 20 TRADING DAYS
        df_last_20 = df.iloc[-20:].copy()
        if len(df_last_20) < 10:
            continue

        # 6. HAR DIN CHECK KARO - SIRF PROPER CTD STORE KARO
        for j in range(len(df_last_20)):
            bo_candle = df_last_20.iloc[j]
            bo_date = bo_candle.name
            df_past = df.loc[:bo_date].iloc[:-1]

            if len(df_past) < 90:
                continue

            # SPRING = Pichle 90 din ka Low
            df_90d = df_past.iloc[-90:].copy()
            spring_low = df_90d['Low'].min()
            spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
            spring_idx = df_90d.index.get_loc(spring_candle.name)

            # CREEK = Spring se pehle ka High
            df_before_spring = df_90d.iloc[:spring_idx+1]
            if df_before_spring.empty:
                continue
            creek_high = df_before_spring['High'].max()

            # CTD CHECK
            vol_condition = bo_candle['Volume'] < bo_candle['Vol_50'] * 1.2
            breakout = bo_candle['Close'] > creek_high

            # SIRF PROPER CTD HO TO SAVE KARO
            if vol_condition and breakout:
                ctd_found.append({
                    'ScanDate': end_date.strftime('%d/%m/%Y'), # Kis din scan kiya
                    'CTD_Date': bo_date.strftime('%d/%m/%Y'), # CTD kab bana
                    'Stock': stock,
                    'Status': 'READY',
                    'SpringDate': spring_candle.name.strftime('%d/%m/%Y'),
                    'SpringLow': round(spring_low, 2),
                    'CreekHigh': round(creek_high, 2),
                    'Close': round(bo_candle['Close'], 2),
                    'Volume': int(bo_candle['Volume']),
                    'Vol_50': int(bo_candle['Vol_50']),
                    'Days_Ago': (end_date - bo_date).days # Kitne din pehle CTD bana
                })
                print(f"[CTD] ✅ {stock}: READY on {bo_date.date()}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. HISTORY SHEET UPDATE + AUTO CREATE + 10 DIN PURANA DATA DELETE
try:
    # Sheet check karo, nahi hai to banao
    try:
        ws_history = sh.worksheet("CTD_History_20D")
    except gspread.WorksheetNotFound:
        print("CTD_History_20D sheet bana raha hu...")
        ws_history = sh.add_worksheet(title="CTD_History_20D", rows="50000", cols="12")
        ws_history.update([['ScanDate','CTD_Date','Stock','Status','SpringDate','SpringLow','CreekHigh','Close','Volume','Vol_50','Days_Ago']])

    # 10 DIN SE PURANA DATA DELETE KARO
    existing_data = ws_history.get_all_records()
    if existing_data:
        df_existing = pd.DataFrame(existing_data)
        df_existing['ScanDate_dt'] = pd.to_datetime(df_existing['ScanDate'], format='%d/%m/%Y')
        cutoff_date = end_date - timedelta(days=10)
        df_existing = df_existing[df_existing['ScanDate_dt'] >= cutoff_date] # Sirf 10 din tak ka rakho
        df_existing = df_existing.drop('ScanDate_dt', axis=1)

        # Sheet clear karke fresh data daalo
        ws_history.clear()
        ws_history.update([df_existing.columns.values.tolist()] + df_existing.values.tolist())
        print(f"=== 10 DIN SE PURANA DATA DELETE KIYA ===")

    # NAYA CTD DATA ADD KARO
    if ctd_found:
        df_new = pd.DataFrame(ctd_found)
        if len(ws_history.get_all_values()) <= 1:
            ws_history.update([df_new.columns.values.tolist()] + df_new.values.tolist())
        else:
            ws_history.append_rows(df_new.values.tolist())
        print(f"\n=== {len(ctd_found)} PROPER CTD FOUND & SAVED ===")
    else:
        print("\n=== LAST 20 DAYS ME KOI PROPER CTD NAHI BANA ===")

except Exception as e:
    print(f"Sheet Error: {e}")

# 8. SUMMARY LIVE SHEET ME
try:
    ws_output = sh.worksheet("LiveSignals")
    ws_output.clear()
    if ctd_found:
        # Unique stocks with latest CTD
        df_summary = pd.DataFrame(ctd_found)
        df_summary = df_summary.sort_values('CTD_Date').drop_duplicates('Stock', keep='last')
        ws_output.update([df_summary.columns.values.tolist()] + df_summary.values.tolist())
        print(f"\n=== SCAN COMPLETE: {len(df_summary)} UNIQUE STOCKS WITH CTD ===")
    else:
        ws_output.update([["No CTD found in last 20 trading days"]])
except Exception as e:
    print(f"LiveSignals Error: {e}")
