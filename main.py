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

print("=== OBV ACCUMULATION BREAKOUT V7.0 ===")

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

# 4. BASE DETECTOR - 3 MAHINE KA DABBA
def is_accumulation_base(df, end_idx, lookback=60, max_range_pct=15):
    if end_idx < lookback:
        return False, None, None

    base_df = df.iloc[end_idx-lookback:end_idx]
    base_high = base_df['High'].max()
    base_low = base_df['Low'].min()
    base_range = (base_high - base_low) / base_low * 100

    # 15% se kam range + kam se kam 40 din base me ho
    if base_range <= max_range_pct and base_range > 3:
        return True, base_high, base_low
    return False, None, None

# 5. MAIN STRATEGY
def find_obv_accumulation_breakout(df_daily, end_date):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 300:
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

        # CONDITION-0: BASE CHECK - SABSE IMPORTANT
        is_base, base_high, base_low = is_accumulation_base(df_daily, week_start_idx, lookback=60, max_range_pct=15)
        if not is_base:
            continue # Base nahi tha to skip

        print(f"DEBUG: BASE FOUND {week_end_date.date()} | Range:{((base_high-base_low)/base_low*100):.1f}% | High:{base_high:.2f} Low:{base_low:.2f}")

        # BASE KE BAAD BREAKOUT DHOONDO
        for j in range(week_start_idx, min(week_start_idx + 20, len(df_daily) - 1)):
            today = df_daily.iloc[j]

            # CONDITION-2: DAILY OBV RISING
            if today['OBV'] <= today['OBV_20DMA']:
                continue

            # CONDITION-3: BASE HIGH BREAKOUT
            if today['Close'] <= base_high:
                continue

            # ENTRY MILI
            entry_price = float(today['Close'])
            sl = float(base_low) # Base ka low hi SL
            risk = entry_price - sl

            if risk <= 0:
                continue

            target = float(entry_price + (risk * 3))

            print(f"DEBUG: ENTRY {today.name.date()} | Base:{base_low:.2f}-{base_high:.2f} | Entry:{entry_price:.2f} | SL:{sl:.2f}")

            # BACKTEST
            exit_price, exit_date, days, result = 0, None, 0, 'Running'
            for k in range(j + 1, len(df_daily)):
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
                'base_high': round(base_high, 2),
                'base_low': round(base_low, 2),
                'base_range_pct': round((base_high-base_low)/base_low*100, 1),
                'entry_price': round(entry_price, 2),
                'sl': round(sl, 2),
                'target': round(target, 2),
                'exit_date': exit_date.strftime('%Y-%m-%d'),
                'exit_price': round(exit_price, 2),
                'days': int(days),
                'pl_pct': round(pl_pct, 2),
                'result': result,
                'risk_pct': round((risk/entry_price)*100, 1)
            })
            break

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

        trades = find_obv_accumulation_breakout(df, ref_date)
        if len(trades) == 0:
            print("No Accumulation Setup")
            continue

        for trade in trades:
            rr = (trade['target'] - trade['entry_price']) / (trade['entry_price'] - trade['sl'])
            print(f" ✅ Base:{trade['base_range_pct']}% | {trade['weekly_crossover']}→{trade['entry_date']} | {trade['result']} | P&L: {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade, 'R:R': round(rr, 1)})

        time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. SHEET UPDATE
try:
    ws_output = sh.worksheet("OBV_Accumulation_Setups")
except:
    ws_output = sh.add_worksheet(title="OBV_Accumulation_Setups", rows=1000, cols=16)

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
    avg_base_range = round(df_out['base_range_pct'].mean(), 1)

    summary = [
        ['', ''], ['STRATEGY', 'Accumulation Base <15% + OBV Cross + Base BO'],
        ['AVG BASE RANGE', f"{avg_base_range}%"], ['TOTAL TRADES', int(total_trades)],
        ['WINS', int(wins)], ['WIN RATE %', float(win_rate)],
        ['TOTAL P&L %', round(total_pl, 2)], ['AVG P&L %', float(avg_pl)]
    ]
    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {len(signals)} TRADES | WIN RATE: {win_rate}% | TOTAL P&L: {total_pl:.1f}% ===")
else:
    ws_output.update('A1', [["No Accumulation Setups Found"]])
    print("\n=== DONE: 0 SETUPS ===")
