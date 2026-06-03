import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF ACCUMULATION SCANNER - FINAL ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. DATE SETUP - AUTO FORMAT DETECT ✅
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
try:
    ref_date = datetime.strptime(date_raw, '%Y-%m-%d')
except ValueError:
    ref_date = datetime.strptime(date_raw, '%d/%m/%Y')

end_date = ref_date
start_date = ref_date - timedelta(days=45)
print(f"Backtest Period: {start_date.date()} to {end_date.date()}")

# 3. NIFTY FOR RS CALCULATION
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. SCAN ALL STOCKS
stocks = [s.strip().upper() for s in ws_watchlist.col_values(1)[1:] if s.strip()]
all_signals = []

for i, stock in enumerate(stocks):
    try:
        df = yf.download(f"{stock}.NS", start=start_date - timedelta(days=100),
                         end=end_date + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue
        df = df[df.index <= end_date]

        for j in range(63, len(df)):
            today = df.iloc[j]

            # ===== STAGE 1: 10 DIN NIKAL DO, USKE PEHLE 20 DIN KA STRUCTURE =====
            if j < 30: continue
            structure_window = df.iloc[j-30:j-10]
            if len(structure_window) < 20: continue

            # ===== STAGE 2A: SWING HIGH HIGHER BAN RAHA KYA =====
            first_half = structure_window.iloc[:10]
            second_half = structure_window.iloc[10:]
            swing_high_1 = first_half['High'].max()
            swing_high_2 = second_half['High'].max()
            if swing_high_2 <= swing_high_1 * 1.005: continue

            # ===== STAGE 2B: SWING LOW HIGHER BAN RAHA KYA =====
            swing_low_1 = first_half['Low'].min()
            swing_low_2 = second_half['Low'].min()
            if swing_low_2 <= swing_low_1 * 1.005: continue

            # ===== STAGE 2C: CLOSE BASIS - ACCUMULATION CANDLE =====
            green_days = (structure_window['Close'] > structure_window['Open']).sum()
            if green_days < 10: continue

            # ===== STAGE 3: LAST 10 DIN FOOTPRINT CHECK =====
            prev_10 = df.iloc[j-10:j]
            ten_day_high = prev_10['High'].max()
            ten_day_max_vol = prev_10['Volume'].max()
            if today['High'] >= ten_day_high: continue
            if today['Volume'] <= ten_day_max_vol: continue

            # ===== STAGE 4: LIQUIDITY + RS CHECK =====
            avg_vol_50 = df['Volume'].iloc[j-50:j].mean()
            if avg_vol_50 < 300000: continue
            vol_multiple = today['Volume'] / avg_vol_50
            if vol_multiple < 2.0: continue

            nifty_idx = nifty_close.index.get_indexer([df.index[j]], method='nearest')[0]
            if nifty_idx < 63: continue
            nifty_ret = nifty_close.iloc[nifty_idx] / nifty_close.iloc[nifty_idx-63] - 1
            stock_ret = today['Close'] / df['Close'].iloc[j-63] - 1
            rs_rating = (stock_ret - nifty_ret) * 100
            if rs_rating < 0: continue

            # ===== SAB PASS ✅ WYCKOFF SIGNAL BANAO =====
            signal_date = today.name
            creek = ten_day_high
            entry = creek * 1.001

            structure_hh = round((swing_high_2/swing_high_1 - 1) * 100, 1)
            structure_hl = round((swing_low_2/swing_low_1 - 1) * 100, 1)
            base_high = structure_window['High'].max()
            base_low = structure_window['Low'].min()
            base_range = (base_high - base_low) / base_low if base_low > 0 else 0

            # 15 DIN AAGE RESULT CHECK
            future_data = df.iloc[j+1:j+16]
            status = "INTACT"
            max_profit = 0
            days_to_breakout = 0

            if len(future_data) > 0:
                for k, row in future_data.iterrows():
                    if row['High'] > entry:
                        days_to_breakout = (k - signal_date).days
                        max_high_15d = future_data.loc[:k]['High'].max()
                        max_profit = round((max_high_15d - entry) / entry * 100, 1)
                        if max_profit >= 6.0: status = "BREAKOUT"
                        elif max_profit >= 3.0: status = "BREAKOUT_WEAK"
                        else: status = "BREAKOUT_SMALL"
                        break

                min_low_15d = future_data['Low'].min()
                if min_low_15d < swing_low_2 * 0.98 and status == "INTACT":
                    status = "FAKEOUT"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Close': round(today['Close'], 2),
                'Creek': round(entry, 2),
                'HH%': structure_hh,
                'HL%': structure_hl,
                'Green': f"{green_days}/20",
                'Base%': round(base_range*100, 1),
                'RS%': round(rs_rating, 1),
                'Vol': f"{vol_multiple:.1f}x",
                'Status': status,
                'Max%': max_profit,
                'Days': days_to_breakout if days_to_breakout > 0 else "-"
            })
            print(f"💎 {signal_date.date()} | {stock} | HH:{structure_hh}% HL:{structure_hl}% | RS:{rs_rating:.1f}% | {status} {max_profit}%")

        time.sleep(0.05)
    except Exception as e:
        print(f"Error {stock}: {str(e)[:40]}")
        pass

# 6. OUTPUT TO SHEET
try:
    ws_output = sh.worksheet("WyckoffSignals")
except:
    ws_output = sh.add_worksheet(title="WyckoffSignals", rows=2000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    breakout_weak = len(df_out[df_out['Status'] == 'BREAKOUT_WEAK'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])

    summary = [
        ["WYCKOFF ACCUMULATION SIGNALS", ""],
        ["Logic: 20D Structure HH+HL + Close>Open + 10D Footprint + RS>0", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Breakout 3-6%", breakout_weak],
        ["Fakeout", fakeout],
        ["Success Rate 6%+", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["Avg Max Profit", f"{round(df_out['Max%'][df_out['Max%']>0].mean(),1)}%" if len(df_out[df_out['Max%']>0]) > 0 else "0%"],
        ["", ""],
    ]
    payload = summary + [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT 6%+ | {round(breakout/total*100,1)}% SUCCESS ===")
else:
    ws_output.update('A1', [["Status", "No Wyckoff Signals Found"]])
    print("\n=== DONE: 0 SIGNALS - Market me structure nahi ban raha ===")
