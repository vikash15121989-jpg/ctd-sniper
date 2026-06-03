import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== SILENT ACCUMULATION V9.5 - BALANCED HIGH GRADE ===")

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

end_date = ref_date
start_date = ref_date - timedelta(days=45)
print(f"Backtest Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

# 3. NIFTY DATA
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. STOCKS
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
all_signals = []

for i, stock in enumerate(stocks):
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

            # BASIC 2 RULE
            ten_day_high = prev_10['High'].max()
            ten_day_max_vol = prev_10['Volume'].max()
            if not (today['High'] < ten_day_high and today['Volume'] > ten_day_max_vol): continue

            # FILTER 1: LIQUIDITY
            avg_vol_50 = df['Volume'].iloc[j-50:j].mean()
            avg_turnover = avg_vol_50 * today['Close']
            if avg_vol_50 < 500000 or avg_turnover < 100000000: continue

            # FILTER 2: BASE 6-14% - BALANCED
            base_high = prev_20['High'].max()
            base_low = prev_20['Low'].min()
            base_range = (base_high - base_low) / base_low
            if not (0.06 <= base_range <= 0.14): continue

            # FILTER 3: SPRING
            recent_low = df['Low'].iloc[j-4:j+1].min()
            if recent_low > base_low * 1.02: continue

            # FILTER 4: CREEK 2% TAK
            if today['Close'] < ten_day_high * 0.98: continue

            # FILTER 5: RS > 8% - BALANCED
            nifty_idx = nifty_close.index.get_indexer([df.index[j]], method='nearest')[0]
            if nifty_idx < 63: continue
            nifty_ret = nifty_close.iloc[nifty_idx] / nifty_close.iloc[nifty_idx-63] - 1
            stock_ret = today['Close'] / df['Close'].iloc[j-63] - 1
            rs_rating = (stock_ret - nifty_ret) * 100
            if rs_rating < 8: continue

            # FILTER 6: VOL > 2.5x - BALANCED
            if today['Volume'] < avg_vol_50 * 2.5: continue

            # FILTER 7: NO DISTRIBUTION
            high_70d_ago = df['High'].iloc[max(0,j-70):j-20].max()
            if high_70d_ago > base_high * 1.25: continue

            # FILTER 8: CLOSE STRENGTH
            daily_range = today['High'] - today['Low']
            if daily_range == 0: continue
            if (today['Close'] - today['Low']) / daily_range < 0.70: continue

            # STATUS CHECK
            signal_date = today.name
            creek = ten_day_high
            entry = creek * 1.001
            sl = recent_low * 0.98
            future_data = df.iloc[j+1:j+6]
            status = "INTACT"

            if len(future_data) > 0:
                max_high_5d = future_data['High'].max()
                min_low_5d = future_data['Low'].min()
                if max_high_5d > creek:
                    profit_pct = (max_high_5d - entry) / entry * 100
                    if profit_pct >= 6.0: status = "BREAKOUT"
                    elif min_low_5d < entry * 0.99: status = "FAKEOUT"
                    else: status = "BREAKOUT_WEAK"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock, 'Base%': round(base_range*100,1), 'RS%': round(rs_rating,1),
                'Vol': f"{round(today['Volume']/avg_vol_50,1)}x",
                'Close': round(today['Close'], 2), 'Creek': round(entry, 2),
                'SL': round(sl, 2), 'Status': status
            })
            print(f" ✅ {signal_date.date()} | {stock} | Base:{base_range*100:.1f}% | RS:{rs_rating:.1f}% | {status}")

        time.sleep(0.05)
    except Exception as e:
        pass

# 6. OUTPUT
try:
    ws_output = sh.worksheet("HighGradeBacktest")
except:
    ws_output = sh.add_worksheet(title="HighGradeBacktest", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])

    summary = [
        ["HIGH GRADE BACKTEST - 45 DAYS", ""],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Fakeout", fakeout],
        ["Success Rate", f"{round(breakout/total*100,1)}%"],
        ["", ""],
    ]
    payload = summary + [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT | {round(breakout/total*100,1)}% SUCCESS ===")
else:
    ws_output.update('A1', [["Status", "No Signals - Filters too tight"]])
    print("\n=== DONE: 0 SIGNALS ===")
