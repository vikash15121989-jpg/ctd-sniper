import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== SILENT ACCUMULATION BACKTEST V1.0 ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. DATE SETUP
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']

ref_date = None
for fmt in date_formats:
    try:
        ref_date = datetime.strptime(date_raw, fmt)
        break
    except ValueError:
        continue

if ref_date is None:
    raise ValueError(f"A1 me date format galat: {date_raw}")

end_date = ref_date
start_date = ref_date - timedelta(days=45) # 1 month + buffer
print(f"Backtest Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

# 3. STOCKS LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

# 4. MAIN BACKTEST LOOP
all_signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start=start_date - timedelta(days=20),
                         end=end_date + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 25: continue

        df = df[df.index <= end_date] # Sirf ref_date tak ka data

        # Har din check karo pichle 1 mahine me
        for j in range(11, len(df)):
            today = df.iloc[j]
            prev_10 = df.iloc[j-10:j] # Aaj exclude, pichle 10 din

            ten_day_high = prev_10['High'].max()
            ten_day_max_vol = prev_10['Volume'].max()

            # RULE 1: High < 10D High
            cond1 = today['High'] < ten_day_high

            # RULE 2: Vol > 10D Max Vol
            cond2 = today['Volume'] > ten_day_max_vol

            if cond1 and cond2:
                signal_date = today.name
                creek = ten_day_high
                entry = creek * 1.001 # Next day entry assumption

                # NEXT 5 DIN ME KYA HUA - STATUS CHECK
                future_data = df.iloc[j+1:j+6] # Next 5 trading days
                status = "INTACT"

                if len(future_data) > 0:
                    max_high_5d = future_data['High'].max()
                    min_low_5d = future_data['Low'].min()

                    # Breakout hua ya nahi?
                    if max_high_5d > creek:
                        # Kitna % diya?
                        profit_pct = (max_high_5d - entry) / entry * 100

                        if profit_pct >= 6.0:
                            status = "BREAKOUT"
                        else:
                            # 6% nahi diya aur neeche aa gaya
                            if min_low_5d < entry * 0.99: # Entry se 1% neeche
                                status = "FAKEOUT"
                            else:
                                status = "BREAKOUT_WEAK" # Tuta but 6% nahi

                all_signals.append({
                    'Date': signal_date.strftime('%Y-%m-%d'),
                    'Stock': stock,
                    'Signal_Close': round(today['Close'], 2),
                    'Signal_High': round(today['High'], 2),
                    'Signal_Vol_Lakh': round(today['Volume']/100000, 1),
                    '10D_Max_Price': round(ten_day_high, 2),
                    '10D_Max_Vol_Lakh': round(ten_day_max_vol/100000, 1),
                    'Creek_Entry': round(entry, 2),
                    'Status': status
                })
                print(f" ✅ {signal_date.date()} | Creek:{ten_day_high:.1f} | Status:{status}")

        time.sleep(0.1) # Rate limit avoid

    except Exception as e:
        print(f"Error {stock}: {e}")

# 5. SHEET UPDATE
try:
    ws_output = sh.worksheet("SilentAccumulation")
except:
    ws_output = sh.add_worksheet(title="SilentAccumulation", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    # Sort: Date DESC, Status priority
    status_order = {'BREAKOUT': 1, 'BREAKOUT_WEAK': 2, 'INTACT': 3, 'FAKEOUT': 4}
    df_out['Status_Order'] = df_out['Status'].map(status_order)
    df_out = df_out.sort_values(['Date', 'Status_Order'], ascending=[False, True])
    df_out = df_out.drop(['Status_Order'], axis=1)

    # Summary stats
    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])
    intact = len(df_out[df_out['Status'] == 'INTACT'])

    summary = [
        ["BACKTEST SUMMARY", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Fakeout", fakeout],
        ["Intact", intact],
        ["Success Rate", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["", ""],
        ["Date", "Stock", "Signal_Close", "Signal_High", "Signal_Vol_Lakh",
         "10D_Max_Price", "10D_Max_Vol_Lakh", "Creek_Entry", "Status"]
    ]

    payload = summary + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT | {fakeout} FAKEOUT ===")
else:
    ws_output.update('A1', [["Status", "No Signals Found Last 30 Days"]])
    print("\n=== DONE: 0 SIGNALS ===")
