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

print("=== OBV SQUEEZE BACKTEST V8.1 - FIXED ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE - MULTI FORMAT SUPPORT
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

# 4. SQUEEZE + BACKTEST COMBO
def find_and_backtest_squeeze(df_daily, end_date):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 300:
        return []

    df_daily = calculate_obv(df_daily)
    df_daily['OBV_20MA'] = df_daily['OBV'].rolling(20).mean()
    df_daily['Vol_20MA'] = df_daily['Volume'].rolling(20).mean()

    trades = []
    i = 60 # 60 din ke baad hi start

    while i < len(df_daily) - 5: # Last 5 din chod do exit ke liye
        today = df_daily.iloc[i]
        week_ago = df_daily.iloc[i-5]

        # CONDITION 1: BASE CHECK - 60 din range
        lookback = df_daily.iloc[i-60:i]
        base_high = lookback['High'].max()
        base_low = lookback['Low'].min()
        base_range_pct = (base_high - base_low) / base_low * 100

        if base_range_pct > 18 or base_range_pct < 3:
            i += 1
            continue

        # CONDITION 2: OBV SQUEEZE
        if pd.isna(today['OBV_20MA']) or today['OBV_20MA'] == 0:
            i += 1
            continue

        obv_vs_ma = (today['OBV'] / today['OBV_20MA'] - 1) * 100
        if not (-5 <= obv_vs_ma <= 2):
            i += 1
            continue

        # CONDITION 3: OBV UPAR MUD RAHA
        if today['OBV'] <= week_ago['OBV']:
            i += 1
            continue

        # CONDITION 4: PRICE BASE KE TOP PE
        if today['Close'] < base_high * 0.95:
            i += 1
            continue

        # CONDITION 5: VOLUME JAGRAHA
        vol_ratio = today['Volume'] / today['Vol_20MA']
        if vol_ratio < 1.2:
            i += 1
            continue

        # SQUEEZE MILA - AGLE DIN ENTRY
        entry_idx = i + 1
        if entry_idx >= len(df_daily):
            break

        entry_day = df_daily.iloc[entry_idx]
        entry_price = float(entry_day['Open']) # Agle din open pe entry
        sl = float(base_low)
        risk = entry_price - sl

        if risk <= 0:
            i += 1
            continue

        # TARGET 1:2 - Bear market ke liye realistic
        target = float(entry_price + (risk * 2))

        # BACKTEST - SL YA TARGET HIT KARO
        exit_price, exit_date, days, result = entry_price, entry_day.name, 0, 'Running'

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

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        trades.append({
            'squeeze_date': today.name.strftime('%Y-%m-%d'),
            'entry_date': entry_day.name.strftime('%Y-%m-%d'),
            'base_range_pct': round(base_range_pct, 1),
            'obv_vs_ma_pct': round(obv_vs_ma, 1),
            'vol_ratio': round(vol_ratio, 1),
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

        # Ek stock me ek time pe ek trade. Next squeeze dhoond
        i = k + 5 # 5 din ka gap next setup ke liye
        continue

    return trades

# 5. MAIN LOOP
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

        trades = find_and_backtest_squeeze(df, ref_date)
        if len(trades) == 0:
            print("No Squeeze Trades")
            continue

        for trade in trades:
            print(f" ✅ {trade['squeeze_date']}→{trade['entry_date']} | Base:{trade['base_range_pct']}% | {trade['result']} | P&L: {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade})

        time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE
try:
    ws_output = sh.worksheet("OBV_Squeeze_Backtest")
except:
    ws_output = sh.add_worksheet(title="OBV_Squeeze_Backtest", rows=2000, cols=15)

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

    # SUMMARY STATS
    total_trades = len(df_out)
    wins = len(df_out[df_out['result'] == 'Target Hit'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    total_pl = round(df_out['pl_pct'].sum(), 2)
    avg_pl = round(total_pl / total_trades, 2) if total_trades > 0 else 0
    avg_base = round(df_out['base_range_pct'].mean(), 1)
    avg_days = round(df_out['days'].mean(), 0)

    # YEAR WISE BREAKDOWN
    df_out['year'] = pd.to_datetime(df_out['entry_date']).dt.year
    year_stats = df_out.groupby('year')['pl_pct'].agg(['count', 'sum', 'mean']).round(2)

    summary = [
        ['', ''], ['STRATEGY', 'OBV Squeeze: Base 3-18% + OBV Cross + Vol 1.2x'],
        ['TARGET RR', '1:2'], ['TOTAL TRADES', int(total_trades)],
        ['WINS', int(wins)], ['WIN RATE %', float(win_rate)],
        ['TOTAL P&L %', float(total_pl)], ['AVG P&L %', float(avg_pl)],
        ['AVG BASE %', float(avg_base)], ['AVG HOLD DAYS', int(avg_days)],
        ['', ''], ['YEAR', 'TRADES', 'TOTAL P&L %', 'AVG P&L %']
    ]

    for year, row in year_stats.iterrows():
        summary.append([int(year), int(row['count']), float(row['sum']), float(row['mean'])])

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {len(signals)} SQUEEZE TRADES | WIN: {win_rate}% | AVG: {avg_pl}% | TOTAL: {total_pl}% ===")
    print("\nYEAR WISE:")
    print(year_stats)
else:
    ws_output.update('A1', [["No Squeeze Backtest Trades Found"]])
    print("\n=== DONE: 0 SETUPS ===")
