import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== ZIGZAG 6% + RELATIVE SWING TIGHT FILTER ===")

# ✅ SETTINGS - YAHAN CHANGE KAR
MIN_SWING_GROWTH = 20.0 # % - Naya swing purane se kitna % bada hona chahiye
MIN_HH = 3.0 # % - Minimum HH%
MIN_HL = 3.0 # % - Minimum HL%

# ZIGZAG 6% - CLOSE BASIS
def get_zigzag_swings(df, pct=6.0):
    highs, lows, closes = df['High'].values, df['Low'].values, df['Close'].values
    swing_highs, swing_lows = [], []
    last_high_idx = last_low_idx = 0
    trend = 0

    for i in range(1, len(df)):
        if trend >= 0:
            if closes[i] > closes[last_high_idx]: last_high_idx = i
            elif (closes[last_high_idx] - closes[i]) / closes[last_high_idx] * 100 >= pct:
                swing_highs.append((last_high_idx, closes[last_high_idx], highs[last_high_idx]))
                trend, last_low_idx = -1, i
        else:
            if closes[i] < closes[last_low_idx]: last_low_idx = i
            elif (closes[i] - closes[last_low_idx]) / closes[last_low_idx] * 100 >= pct:
                swing_lows.append((last_low_idx, closes[last_low_idx], lows[last_low_idx]))
                trend, last_high_idx = 1, i

    if trend >= 0: swing_highs.append((last_high_idx, closes[last_high_idx], highs[last_high_idx]))
    else: swing_lows.append((last_low_idx, closes[last_low_idx], lows[last_low_idx]))
    return swing_highs, swing_lows

# SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# DATE
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
try: ref_date = datetime.strptime(date_raw, '%Y-%m-%d')
except: ref_date = datetime.strptime(date_raw, '%d/%m/%Y')
end_date, start_date = ref_date, ref_date - timedelta(days=60)
print(f"Period: {start_date.date()} to {end_date.date()}")

# SCAN
stocks = [s.strip().upper() for s in ws_watchlist.col_values(1)[1:] if s.strip()]
all_signals = []

for stock in stocks:
    try:
        df = yf.download(f"{stock}.NS", start=start_date - timedelta(days=150),
                         end=end_date + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue
        df = df[df.index <= end_date]

        swing_highs, swing_lows = get_zigzag_swings(df, pct=6.0)
        if len(swing_highs) < 2 or len(swing_lows) < 2: continue

        last_used_sh2_idx = -1
        last_used_sl2_idx = -1

        for j in range(50, len(df)):
            past_sh = [sh for sh in swing_highs if sh[0] < j]
            past_sl = [sl for sl in swing_lows if sl[0] < j]
            if len(past_sh) < 2 or len(past_sl) < 2: continue

            sh1_idx, sh1_close, sh1_high = past_sh[-2]
            sh2_idx, sh2_close, sh2_high = past_sh[-1]
            sl1_idx, sl1_close, sl1_low = past_sl[-2]
            sl2_idx, sl2_close, sl2_low = past_sl[-1]

            # Close basis HH-HL + Minimum %
            hh_pct = (sh2_close/sh1_close - 1) * 100
            hl_pct = (sl2_close/sl1_close - 1) * 100
            if hh_pct < MIN_HH or hl_pct < MIN_HL: continue

            # ✅ RELATIVE SWING TIGHT FILTER
            swing1_size = sh1_close - sl1_close
            swing2_size = sh2_close - sl2_close
            swing_growth = (swing2_size/swing1_size - 1) * 100
            if swing_growth < MIN_SWING_GROWTH: continue # 20% se kam growth to skip

            # Ek structure = Ek signal
            if sh2_idx == last_used_sh2_idx and sl2_idx == last_used_sl2_idx:
                continue

            # FOOTPRINT AFTER HH
            if j <= sh2_idx: continue

            prev_10 = df.iloc[j-10:j]
            if len(prev_10) < 10: continue
            today = df.iloc[j]

            if today['Volume'] <= prev_10['Volume'].max(): continue
            if today['High'] >= prev_10['High'].max(): continue

            # SIGNAL CONFIRM
            last_used_sh2_idx = sh2_idx
            last_used_sl2_idx = sl2_idx

            signal_date = today.name
            vol_multiple = round(today['Volume'] / prev_10['Volume'].max(), 1)
            creek = prev_10['High'].max()
            entry = creek * 1.001

            future_data = df.iloc[j+1:j+16]
            status = "INTACT"
            max_profit = 0
            days_to_breakout = 0

            if len(future_data) > 0:
                breakout_idx = future_data[future_data['High'] > entry].index
                if len(breakout_idx) > 0:
                    first_breakout = breakout_idx[0]
                    days_to_breakout = (first_breakout - signal_date).days
                    max_high_15d = future_data.loc[:first_breakout]['High'].max()
                    max_profit = round((max_high_15d - entry) / entry * 100, 1)
                    if max_profit >= 6.0: status = "BREAKOUT"
                    elif max_profit >= 3.0: status = "BREAKOUT_WEAK"
                    else: status = "BREAKOUT_SMALL"

                min_low_15d = future_data['Low'].min()
                if min_low_15d < sl2_low * 0.98 and status == "INTACT":
                    status = "FAKEOUT"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Close': round(today['Close'], 2),
                'Creek': round(creek, 2),
                'HH%': round(hh_pct, 1),
                'HL%': round(hl_pct, 1),
                'SwingGrow%': round(swing_growth, 1),
                'Vol_x': vol_multiple,
                'Status': status,
                'Max%': max_profit,
                'Days': days_to_breakout if days_to_breakout > 0 else "-"
            })
            print(f"💎 {signal_date.date()} | {stock} | HH:{hh_pct:.1f}% HL:{hl_pct:.1f}% Swing:{swing_growth:.1f}% | {status} {max_profit}%")

        time.sleep(0.05)
    except Exception as e:
        print(f"Error {stock}: {str(e)[:50]}")

# OUTPUT
try: ws_output = sh.worksheet("WyckoffSignals")
except: ws_output = sh.add_worksheet(title="WyckoffSignals", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    df_out = df_out.replace([np.inf, -np.inf], np.nan)
    df_out = df_out.fillna('')
    df_out['Date'] = pd.to_datetime(df_out['Date'])
    df_out = df_out.sort_values('Date', ascending=False)

    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])

    final_payload = [
        [f"ZIGZAG 6% + RELATIVE SWING >{MIN_SWING_GROWTH}%", ""],
        [f"Rule: Close HH>{MIN_HH}% + HL>{MIN_HL}% + Swing Grow >{MIN_SWING_GROWTH}% + Footprint", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Fakeout", fakeout],
        ["Success Rate 6%+", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["Avg Swing Growth", f"{round(df_out['SwingGrow%'].mean(),1)}%"],
        ["", ""],
    ]

    unique_dates = df_out['Date'].dt.strftime('%Y-%m-%d').unique()

    for date_str in unique_dates:
        date_df = df_out[df_out['Date'].dt.strftime('%Y-%m-%d') == date_str]
        d_total = len(date_df)
        d_breakout = len(date_df[date_df['Status'] == 'BREAKOUT'])
        d_fakeout = len(date_df[date_df['Status'] == 'FAKEOUT'])
        d_success = round(d_breakout/d_total*100,1) if d_total > 0 else 0

        final_payload.append([f"DATE: {date_str}", f"Total: {d_total} | Breakout: {d_breakout} | Fakeout: {d_fakeout} | Success: {d_success}%"])
        final_payload.append(["Stock", "Close", "Creek", "HH%", "HL%", "SwingGrow%", "Vol_x", "Status", "Max%", "Days"])

        for _, row in date_df.iterrows():
            final_payload.append([
                row['Stock'], row['Close'], row['Creek'], row['HH%'], row['HL%'], row['SwingGrow%'],
                row['Vol_x'], row['Status'], row['Max%'], row['Days']
            ])
        final_payload.append(["", ""])

    ws_output.update('A1', final_payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT 6%+ ===")
else:
    ws_output.update('A1', [["Status", "No Signals"]])
