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

print("=== POSITIVE EXPECTANCY SCANNER ===")

# ✅ EXPECTANCY SETTINGS
MIN_HH = 4.0
MIN_HL = 3.0
MIN_SWING_GROWTH = 10.0
VOL_MULTIPLIER = 2.0 # 2x volume
SL_PCT = 1.0 # 1% SL
TARGET_1_PCT = 6.0 # 6% pe 50% book
TARGET_2_PCT = 12.0 # 12% pe trail
TIME_EXIT_DAYS = 5 # 5 din me exit if no breakout

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

date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
try: ref_date = datetime.strptime(date_raw, '%Y-%m-%d')
except: ref_date = datetime.strptime(date_raw, '%d/%m/%Y')
end_date, start_date = ref_date, ref_date - timedelta(days=90)
print(f"Period: {start_date.date()} to {end_date.date()}")

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

            hh_pct = (sh2_close/sh1_close - 1) * 100
            hl_pct = (sl2_close/sl1_close - 1) * 100
            if hh_pct < MIN_HH or hl_pct < MIN_HL: continue

            swing1_size = sh1_close - sl1_close
            swing2_size = sh2_close - sl2_close
            swing_growth = (swing2_size/swing1_size - 1) * 100
            if swing_growth < MIN_SWING_GROWTH: continue

            if sh2_idx == last_used_sh2_idx and sl2_idx == last_used_sl2_idx:
                continue

            prev_10 = df.iloc[j-10:j]
            if len(prev_10) < 10: continue
            today = df.iloc[j]

            # ENTRY: Volume spike + High intact YA Gap-up breakout dono
            vol_condition = today['Volume'] > prev_10['Volume'].max() * VOL_MULTIPLIER
            high_condition = today['High'] < prev_10['High'].max()
            gapup_condition = today['Open'] > prev_10['High'].max() * 1.02 # 2% gap-up

            if not (vol_condition and (high_condition or gapup_condition)): continue

            last_used_sh2_idx = sh2_idx
            last_used_sl2_idx = sl2_idx

            signal_date = today.name
            creek = prev_10['High'].max()
            entry = max(today['Close'], creek * 1.001) # Gap-up me close pe entry
            sl = min(entry * (1 - SL_PCT/100), sl2_low * 0.995) # 1% ya HL ke neeche
            target1 = entry * (1 + TARGET_1_PCT/100)
            target2 = entry * (1 + TARGET_2_PCT/100)

            # 15 DIN SIMULATION WITH SMART EXIT
            future_data = df.iloc[j+1:j+16]
            status = "TIME_EXIT"
            pnl_pct = 0
            exit_reason = f"No breakout in {TIME_EXIT_DAYS}D"

            if len(future_data) > 0:
                for k, row in future_data.iterrows():
                    days_passed = (k - signal_date).days

                    # 1. SL Hit
                    if row['Low'] <= sl:
                        status = "SL_HIT"
                        pnl_pct = -SL_PCT
                        exit_reason = "SL Hit"
                        break

                    # 2. Target 2 Hit
                    if row['High'] >= target2:
                        status = "TARGET_2"
                        pnl_pct = (TARGET_1_PCT * 0.5) + (TARGET_2_PCT * 0.5) # 50% 6%, 50% 12%
                        exit_reason = "Target 2"
                        break

                    # 3. Target 1 Hit - trail SL to entry
                    if row['High'] >= target1:
                        sl = entry # Breakeven
                        status = "TARGET_1"
                        pnl_pct = TARGET_1_PCT * 0.5 # 50% book
                        exit_reason = "Target 1, Trailed"

                    # 4. Time Exit - 5 din me creek nahi toda
                    if days_passed >= TIME_EXIT_DAYS and row['High'] < creek:
                        status = "TIME_EXIT"
                        pnl_pct = (row['Close']/entry - 1) * 100 # Jo mila
                        exit_reason = f"Time Exit {days_passed}D"
                        break

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Entry': round(entry, 2),
                'SL': round(sl, 2),
                'HH%': round(hh_pct, 1),
                'HL%': round(hl_pct, 1),
                'Vol_x': round(today['Volume'] / prev_10['Volume'].max(), 1),
                'Status': status,
                'P&L%': round(pnl_pct, 1),
                'Reason': exit_reason
            })
            print(f"💰 {signal_date.date()} | {stock} | Entry:{entry:.1f} | {status} {pnl_pct:.1f}% | {exit_reason}")

        time.sleep(0.05)
    except Exception as e:
        print(f"Error {stock}: {str(e)[:50]}")

# OUTPUT + EXPECTANCY CALC
try: ws_output = sh.worksheet("ExpectancySignals")
except: ws_output = sh.add_worksheet(title="ExpectancySignals", rows=5000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    df_out = df_out.replace([np.inf, -np.inf], np.nan).fillna('')

    total = len(df_out)
    wins = len(df_out[df_out['P&L%'] > 0])
    losses = len(df_out[df_out['P&L%'] < 0])
    win_rate = round(wins/total*100, 1) if total > 0 else 0
    avg_win = round(df_out[df_out['P&L%'] > 0]['P&L%'].mean(), 1) if wins > 0 else 0
    avg_loss = round(df_out[df_out['P&L%'] < 0]['P&L%'].mean(), 1) if losses > 0 else 0
    expectancy = round((win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss), 2)
    net_pnl = round(df_out['P&L%'].sum(), 1)

    final_payload = [
        ["POSITIVE EXPECTANCY STRATEGY", ""],
        [f"SL:{SL_PCT}% T1:{TARGET_1_PCT}% T2:{TARGET_2_PCT}% TimeExit:{TIME_EXIT_DAYS}D", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["", ""],
        ["=== EXPECTANCY MATH ===", ""],
        ["Total Trades", total],
        ["Win Rate", f"{win_rate}%"],
        ["Avg Win", f"{avg_win}%"],
        ["Avg Loss", f"{avg_loss}%"],
        ["Expectancy/Trade", f"{expectancy}%"],
        ["Total Net P&L", f"{net_pnl}%"],
        ["R:R", f"1:{round(abs(avg_win/avg_loss),1)}" if avg_loss!= 0 else "N/A"],
        ["", ""],
        ["Date", "Stock", "Entry", "SL", "HH%", "HL%", "Vol_x", "Status", "P&L%", "Reason"]
    ]

    for _, row in df_out.sort_values('Date', ascending=False).iterrows():
        final_payload.append([row['Date'], row['Stock'], row['Entry'], row['SL'], row['HH%'],
                             row['HL%'], row['Vol_x'], row['Status'], row['P&L%'], row['Reason']])

    ws_output.update('A1', final_payload)
    print(f"\n=== DONE: {total} Trades | Win:{win_rate}% | Expectancy:{expectancy}%/trade | Net:{net_pnl}% ===")
else:
    ws_output.update('A1', [["Status", "No Signals"]])
