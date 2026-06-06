import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== OBV WEEKLY CROSSOVER + SWING BREAKOUT V6.1 ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE
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

date_str = ref_date.strftime('%Y-%m-%d')
print(f"Backtest Till Date: {date_str}")

# 3. OBV CALCULATOR
def calculate_obv(df):
    obv = [0]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            obv.append(obv[-1] + df['Volume'].iloc[i])
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            obv.append(obv[-1] - df['Volume'].iloc[i])
        else:
            obv.append(obv[-1])
    df['OBV'] = obv
    return df

# 4. SWING POINTS FINDER
def find_swing_points(df, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(df) - right):
        # Swing High: middle candle highest
        if df['High'].iloc[i] == df['High'].iloc[i-left:i+right+1].max():
            highs.append({'idx': i, 'price': df['High'].iloc[i], 'date': df.index[i]})
        # Swing Low: middle candle lowest
        if df['Low'].iloc[i] == df['Low'].iloc[i-left:i+right+1].min():
            lows.append({'idx': i, 'price': df['Low'].iloc[i], 'date': df.index[i]})
    return highs, lows

# 5. MAIN STRATEGY
def find_obv_swing_breakout(df_daily, end_date):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 250:
        return []

    # WEEKLY DATA
    df_weekly = df_daily.resample('W-FRI').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    }).dropna()

    if len(df_weekly) < 30:
        return []

    # WEEKLY OBV + 20MA
    df_weekly = calculate_obv(df_weekly)
    df_weekly['OBV_20MA'] = df_weekly['OBV'].rolling(20).mean()

    # DAILY OBV + 20DMA
    df_daily = calculate_obv(df_daily)
    df_daily['OBV_20DMA'] = df_daily['OBV'].rolling(20).mean()

    # SWING POINTS
    swing_highs, swing_lows = find_swing_points(df_daily, left=2, right=2)

    setups = []

    # WEEKLY CROSSOVER CHECK
    for i in range(21, len(df_weekly)):
        prev_wk = df_weekly.iloc[i-1]
        curr_wk = df_weekly.iloc[i]

        # CONDITION-1: WEEKLY OBV CROSSOVER
        if pd.isna(prev_wk['OBV_20MA']) or pd.isna(curr_wk['OBV_20MA']):
            continue
        if not (prev_wk['OBV'] < prev_wk['OBV_20MA'] and curr_wk['OBV'] > curr_wk['OBV_20MA']):
            continue

        week_end_date = curr_wk.name
        week_start_idx = df_daily.index.get_indexer([week_end_date], method='bfill')[0]

        # Us week ke baad 15 trading days me entry dhoondo
        for j in range(week_start_idx, min(week_start_idx + 15, len(df_daily) - 1)):
            today = df_daily.iloc[j]

            # CONDITION-2: DAILY OBV RISING
            if today['OBV'] <= today['OBV_20DMA']:
                continue

            # CONDITION-3: SWING HIGH BREAKOUT
            # Recent swing high nikalo
            recent_sh = [sh for sh in swing_highs if sh['idx'] < j]
            if not recent_sh:
                continue
            last_swing_high = recent_sh[-1]['price']

            # Breakout hua kya?
            if today['Close'] <= last_swing_high:
                continue

            # CONDITION-4: SL = RECENT SWING LOW
            recent_sl = [sl for sl in swing_lows if sl['idx'] < j]
            if not recent_sl:
                continue
            last_swing_low = recent_sl[-1]['price']

            # ENTRY MILI
            entry_idx = j
            entry_price = float(today['Close'])
            sl = float(last_swing_low * 0.99) # 1% buffer niche

            risk = entry_price - sl
            if risk <= 0 or risk/entry_price > 0.15: # 15% se zyada risk nahi
                continue

            target = float(entry_price + (risk * 3))

            # DEBUG LOG
            print(f"DEBUG: Entry {today.name.date()} | WkCross {week_end_date.date()} | SH:{last_swing_high:.2f} | SL:{sl:.2f} | Entry:{entry_price:.2f}")

            # BACKTEST
            exit_price, exit_date, days, result = 0, None, 0, 'Running'
            for k in range(entry_idx + 1, len(df_daily)):
                days += 1
                h, l, c = df_daily['High'].iloc[k], df_daily['Low'].iloc[k], df_daily['Close'].iloc[k]
                if l <= sl:
                    exit_price, exit_date, result = sl, df_daily.index[k], 'SL Hit'
                    break
                if h >= target:
                    exit_price, exit_date, result = target, df_daily.index[k], 'Target Hit'
                    break
                if k == len(df_daily) - 1:
                    exit_price, exit_date, result = float(c), df_daily.index[k], 'Running'

            if exit_date is None:
                continue

            pl_pct = ((exit_price - entry_price) / entry_price) * 100

            setups.append({
                'entry_date': today.name.strftime('%Y-%m-%d'),
                'weekly_crossover': week_end_date.strftime('%Y-%m-%d'),
                'entry_price': round(entry_price, 2),
                'swing_high': round(last_swing_high, 2),
                'sl': round(sl, 2),
                'swing_low': round(last_swing_low, 2),
                'target': round(target, 2),
                'exit_date': exit_date.strftime('%Y-%m-%d'),
                'exit_price': round(exit_price, 2),
                'days': int(days),
                'pl_pct': round(pl_pct, 2),
                'result': result,
                'risk_pct': round((risk/entry_price)*100, 1)
            })
            break # Ek crossover pe ek entry

    return setups

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="5y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 300: continue

        trades = find_obv_swing_breakout(df, ref_date)
        if len(trades) == 0:
            print("No OBV Swing Setup")
            continue

        for trade in trades:
            rr = (trade['target'] - trade['entry_price']) / (trade['entry_price'] - trade['sl'])
            print(f" ✅ {trade['weekly_crossover']}→{trade['entry_date']} | SH:{trade['swing_high']} | {trade['result']} | P&L: {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade, 'R:R': round(rr, 1)})

        time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. SHEET UPDATE
try:
    ws_output = sh.worksheet("OBV_Swing_Setups")
except:
    ws_output = sh.add_worksheet(title="OBV_Swing_Setups", rows=1000, cols=16)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('entry_date', ascending=False)

    def convert_to_native(val):
        if isinstance(val, (np.integer, np.int64)): return int(val)
        elif isinstance(val, (np.floating, np.float64)): return float(val)
        else: return val

    df_out = df_out.applymap(convert_to_native)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    total_trades = len(df_out)
    wins = len(df_out[df_out['result'] == 'Target Hit'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    total_pl = float(pd.Series(df_out['pl_pct']).astype(float).sum())
    avg_pl = round(total_pl / total_trades, 2) if total_trades > 0 else 0

    summary = [
        ['', ''], ['STRATEGY', 'Weekly OBV Cross + Daily OBV Rise + Swing High Breakout'],
        ['SL', 'Recent Swing Low'], ['TOTAL TRADES', int(total_trades)],
        ['WINS', int(wins)], ['WIN RATE %', float(win_rate)],
        ['TOTAL P&L %', round(total_pl, 2)], ['AVG P&L %', float(avg_pl)]
    ]
    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {len(signals)} TRADES | WIN RATE: {win_rate}% | TOTAL P&L: {total_pl:.1f}% ===")
else:
    ws_output.update('A1', [["No Setups Found"]])
    print("\n=== DONE: 0 SETUPS ===")
