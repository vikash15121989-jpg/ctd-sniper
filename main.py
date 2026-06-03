import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== ZIGZAG 6% WYCKOFF - RECENT DATE FIRST ===")

# ZIGZAG 6% SWING
def get_zigzag_swings(df, pct=6.0):
    highs, lows, closes = df['High'].values, df['Low'].values, df['Close'].values
    swing_highs, swing_lows = [], []
    last_high_idx = last_low_idx = 0
    trend = 0

    for i in range(1, len(df)):
        if trend >= 0:
            if closes[i] > highs[last_high_idx]: last_high_idx = i
            elif (highs[last_high_idx] - lows[i]) / highs[last_high_idx] * 100 >= pct:
                swing_highs.append((last_high_idx, highs[last_high_idx]))
                trend, last_low_idx = -1, i
        else:
            if closes[i] < lows[last_low_idx]: last_low_idx = i
            elif (highs[i] - lows[last_low_idx]) / lows[last_low_idx] * 100 >= pct:
                swing_lows.append((last_low_idx, lows[last_low_idx]))
                trend, last_high_idx = 1, i

    if trend >= 0: swing_highs.append((last_high_idx, highs[last_high_idx]))
    else: swing_lows.append((last_low_idx, lows[last_low_idx]))
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

        for j in range(50, len(df)):
            past_sh = [sh for sh in swing_highs if sh[0] < j]
            past_sl = [sl for sl in swing_lows if sl[0] < j]
            if len(past_sh) < 2 or len(past_sl) < 2: continue

            sh1, sh2 = past_sh[-2][1], past_sh[-1][1]
            sl1, sl2 = past_sl[-2][1], past_sl[-1][1]
            if sh2 <= sh1 or sl2 <= sl1: continue

            prev_10 = df.iloc[j-10:j]
            if len(prev_10) < 10: continue
            today = df.iloc[j]

            if today['Volume'] <= prev_10['Volume'].max(): continue
            if today['High'] >= prev_10['High'].max(): continue

            signal_date = today.name
            hh_pct = round((sh2/sh1 - 1) * 100, 1)
            hl_pct = round((sl2/sl1 - 1) * 100, 1)
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
                if min_low_15d < sl2 * 0.98 and status == "INTACT":
                    status = "FAKEOUT"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Close': round(today['Close'], 2),
                'Creek': round(creek, 2),
                'HH%': hh_pct,
                'HL%': hl_pct,
                'SH1': round(sh1, 2),
                'SH2': round(sh2, 2),
                'SL1': round(sl1, 2),
                'SL2': round(sl2, 2),
                'Vol_x': vol_multiple,
                'Status': status,
                'Max%': max_profit,
                'Days': days_to_breakout if days_to_breakout > 0 else "-"
            })
            print(f"💎 {signal_date.date()} | {stock} | {status} {max_profit}%")

        time.sleep(0.05)
    except: pass

# OUTPUT - RECENT DATE FIRST
try: ws_output = sh.worksheet("WyckoffSignals")
except: ws_output = sh.add_worksheet(title="WyckoffSignals", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    df_out['Date'] = pd.to_datetime(df_out['Date'])
    df_out = df_out.sort_values('Date', ascending=False) # ✅ RECENT UPAR

    # OVERALL SUMMARY
    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])
    breakout_weak = len(df_out[df_out['Status'] == 'BREAKOUT_WEAK'])
    breakout_small = len(df_out[df_out['Status'] == 'BREAKOUT_SMALL'])
    fakeout = len(df_out[df_out['Status'] == 'FAKEOUT'])
    intact = len(df_out[df_out['Status'] == 'INTACT'])

    final_payload = [
        ["ZIGZAG 6% WYCKOFF - RECENT FIRST", ""],
        ["Rule: 6% Swing HH+HL + Vol>10D MaxVol + High<10D High", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Breakout 3-6%", breakout_weak],
        ["Breakout 0-3%", breakout_small],
        ["Fakeout", fakeout],
        ["Intact", intact],
        ["Success Rate 6%+", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["", ""],
    ]

    # DATE WISE - RECENT FIRST
    unique_dates = df_out['Date'].dt.strftime('%Y-%m-%d').unique() # Already sorted

    for date_str in unique_dates:
        date_df = df_out[df_out['Date'].dt.strftime('%Y-%m-%d') == date_str]
        d_total = len(date_df)
        d_breakout = len(date_df[date_df['Status'] == 'BREAKOUT'])
        d_fakeout = len(date_df[date_df['Status'] == 'FAKEOUT'])
        d_success = round(d_breakout/d_total*100,1) if d_total > 0 else 0

        final_payload.append([f"DATE: {date_str}", f"Total: {d_total} | Breakout: {d_breakout} | Fakeout: {d_fakeout} | Success: {d_success}%"])
        final_payload.append(["Stock", "Close", "Creek", "HH%", "HL%", "Vol_x", "Status", "Max%", "Days"])

        for _, row in date_df.iterrows():
            final_payload.append([
                row['Stock'], row['Close'], row['Creek'], row['HH%'], row['HL%'],
                row['Vol_x'], row['Status'], row['Max%'], row['Days']
            ])
        final_payload.append(["", ""])

    ws_output.update('A1', final_payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT 6%+ ===")
else:
    ws_output.update('A1', [["Status", "No Signals"]])
