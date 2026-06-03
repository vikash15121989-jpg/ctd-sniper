import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== SILENT ACCUMULATION V9.0 - EXACT SHEET FILTER ===")

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
start_date = ref_date - timedelta(days=45)
print(f"Backtest Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

# 3. NIFTY DATA FOR RS
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. STOCKS LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

# 5. MAIN BACKTEST LOOP - 8 FILTER WALE
all_signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start=start_date - timedelta(days=100),
                         end=end_date + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue

        df = df[df.index <= end_date]

        for j in range(21, len(df)):
            today = df.iloc[j]
            prev_10 = df.iloc[j-10:j]
            prev_20 = df.iloc[j-20:j]

            # ===== TERE 2 BASIC RULE =====
            ten_day_high = prev_10['High'].max()
            ten_day_max_vol = prev_10['Volume'].max()
            cond1 = today['High'] < ten_day_high
            cond2 = today['Volume'] > ten_day_max_vol
            if not (cond1 and cond2): continue

            # ===== SHEET KE 6 EXTRA FILTER =====
            # FILTER 1: LIQUIDITY - 5L shares ya 10Cr turnover min
            avg_vol_50 = df['Volume'].iloc[j-50:j].mean()
            avg_turnover = avg_vol_50 * today['Close']
            if avg_vol_50 < 500000 or avg_turnover < 100000000: continue

            # FILTER 2: TIGHT BASE 7-14% - Energy stored
            base_high = prev_20['High'].max()
            base_low = prev_20['Low'].min()
            base_range = (base_high - base_low) / base_low
            if not (0.07 <= base_range <= 0.14): continue

            # FILTER 3: SPRING MANDATORY - Weak hands out
            recent_low = df['Low'].iloc[j-4:j+1].min()
            if recent_low > base_low * 1.02: continue

            # FILTER 4: CREEK KE PAAS - Blast ready
            if today['Close'] < ten_day_high * 0.985: continue

            # FILTER 5: RS RATING > 5% - Nifty se strong
            nifty_idx = nifty_close.index.get_indexer([df.index[j]], method='nearest')[0]
            if nifty_idx < 63: continue
            nifty_ret = nifty_close.iloc[nifty_idx] / nifty_close.iloc[nifty_idx-63] - 1
            stock_ret = today['Close'] / df['Close'].iloc[j-63] - 1
            rs_rating = (stock_ret - nifty_ret) * 100
            if rs_rating < 5: continue

            # FILTER 6: NO DISTRIBUTION - Fresh maal
            high_70d_ago = df['High'].iloc[max(0,j-70):j-20].max()
            if high_70d_ago > base_high * 1.25: continue

            # ===== STATUS CHECK NEXT 5 DIN =====
            signal_date = today.name
            creek = ten_day_high
            entry = creek * 1.001
            spring_low = df['Low'].iloc[j-4:j+1].min()
            sl = spring_low * 0.98

            future_data = df.iloc[j+1:j+6]
            status = "INTACT"

            if len(future_data) > 0:
                max_high_5d = future_data['High'].max()
                min_low_5d = future_data['Low'].min()

                if max_high_5d > creek:
                    profit_pct = (max_high_5d - entry) / entry * 100
                    if profit_pct >= 6.0:
                        status = "BREAKOUT"
                    elif min_low_5d < entry * 0.99:
                        status = "FAKEOUT"
                    else:
                        status = "BREAKOUT_WEAK"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Base_Range%': round(base_range*100,1),
                'RS_Rating%': round(rs_rating,1),
                'Signal_Close': round(today['Close'], 2),
                'Signal_Vol_Lakh': round(today['Volume']/100000, 1),
                '10D_Max_Price': round(ten_day_high, 2),
                'Creek_Entry': round(entry, 2),
                'SL': round(sl, 2),
                'Status': status
            })
            print(f" ✅ {signal_date.date()} | Base:{base_range*100:.1f}% | RS:{rs_rating:.1f}% | {status}")

        time.sleep(0.05)

    except Exception as e:
        print(f"Error {stock}: {e}")

# 6. SHEET UPDATE
try:
    ws_output = sh.worksheet("SheetMatch")
except:
    ws_output = sh.add_worksheet(title="SheetMatch", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    status_order = {'BREAKOUT': 1, 'BREAKOUT_WEAK': 2, 'INTACT': 3, 'FAKEOUT': 4}
    df_out['Status_Order'] = df_out['Status'].map(status_order)
    df_out = df_out.sort_values(['Date', 'Status_Order'], ascending=[False, True])
    df_out = df_out.drop(['Status_Order'], axis=1)

    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])

    summary = [
        ["BACKTEST SUMMARY - EXACT SHEET FILTER", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Fakeout", fakeout],
        ["Success Rate", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["", ""],
    ]

    payload = summary + [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT | Success: {round(breakout/total*100,1)}% ===")
else:
    ws_output.update('A1', [["Status", "No Signals Found"]])
    print("\n=== DONE: 0 SIGNALS ===")
