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

print("=== V10.3E NO HERO LEFT BEHIND ===")

# 1. SETUP
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']
ref_date = None
for fmt in date_formats:
    try:
        ref_date = datetime.strptime(date_raw, fmt)
        break
    except ValueError:
        continue

print(f"Scan Till: {ref_date.date()}")

# 2. NIFTY CACHE
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)

# 3. RULES - ULTRA LOOSE AB
R = {
    'rs_normal': 0.8, # Nifty se 20% kam bhi chalega
    'rs_hero': 1.5, # 2.0 se 1.5
    'rs_god': 3.0, # 4.0 se 3.0
    'extension': 50, # 35 se 50 - Bohot door bhi chalega
    'base_min': 1, # 2 se 1
    'base_max_normal': 25, # 20 se 25
    'base_max_hero': 20, # 15 se 20
    'base_max_god': 30, # 25 se 30
    'vol_normal': 1.0, # 1.2 se 1.0 - Avg volume bhi chalega
    'vol_hero': 1.2, # 1.5 se 1.2
    'vol_god': 1.0, # 1.2 se 1.0
}

# 4. MULTI TIMEFRAME RS - 1M 3M 6M
def check_relative_strength(stock_df, check_date):
    try:
        periods = {'1M': 21, '3M': 63, '6M': 126}
        best_rs = 0
        best_stock_ret = 0
        best_nifty_ret = 0

        for period_name, days in periods.items():
            stock_window = stock_df.loc[:check_date].iloc[-days:]['Close']
            nifty_window = nifty_df.loc[:check_date].iloc[-days:]['Close']

            if len(stock_window) < days * 0.8: continue # 80% data chahiye

            stock_ret = (stock_window.iloc[-1] / stock_window.iloc[0] - 1) * 100
            nifty_ret = (nifty_window.iloc[-1] / nifty_window.iloc[0] - 1) * 100

            if nifty_ret <= 0:
                rs = (stock_ret - nifty_ret) / 10 if stock_ret > nifty_ret else 0
            else:
                rs = stock_ret / nifty_ret

            if rs > best_rs:
                best_rs = rs
                best_stock_ret = stock_ret
                best_nifty_ret = nifty_ret

        # RS Grade
        if best_rs >= R['rs_god'] or (best_stock_ret > 10 and best_nifty_ret < -5):
            grade = 'GOD'
            rs_ok = True
        elif best_rs >= R['rs_hero'] or (best_stock_ret > 5 and best_nifty_ret < 0):
            grade = 'HERO'
            rs_ok = True
        elif best_rs >= R['rs_normal']:
            grade = 'NORMAL'
            rs_ok = True
        else:
            grade = 'WEAK'
            rs_ok = False

        return rs_ok, grade, round(best_stock_ret, 1), round(best_nifty_ret, 1), round(best_rs, 2)
    except:
        return False, 'WEAK', 0, 0, 0

def check_base_breakout(df, idx, rs_grade):
    """FIX: 3 DIN KA BREAKOUT WINDOW"""
    # Pichle 3 din me kabhi bhi base ke upar close hua ho
    window = df.iloc[idx-2:idx+1] # Aaj + 2 din pehle
    lookback = df.iloc[idx-60:idx]

    if len(lookback) < 20: return False, False, 0, 0, 0, 0

    base_high, base_low = lookback['High'].max(), lookback['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100

    if rs_grade == 'GOD':
        base_max = R['base_max_god']
    elif rs_grade == 'HERO':
        base_max = R['base_max_hero']
    else:
        base_max = R['base_max_normal']

    tight_base = R['base_min'] <= base_range_pct <= base_max

    # BREAKOUT: 3 din me kahin bhi base_high cross hua ho
    breakout = (window['Close'] > base_high * 0.98).any()
    near_high = df['Close'].iloc[idx] >= base_high * 0.80 # 80% tak loose

    return tight_base, breakout, near_high, base_high, base_low, round(base_range_pct, 1), base_max

def check_buyer_dominance(df, idx, rs_grade):
    """FIX: 3 DIN KA VOLUME AVG"""
    window = df.iloc[idx-2:idx+1] # 3 din
    avg_vol_3d = window['Volume'].mean()
    vol_20ma = df['Vol_20MA'].iloc[idx]

    if rs_grade == 'GOD':
        vol_needed = R['vol_god']
    elif rs_grade == 'HERO':
        vol_needed = R['vol_hero']
    else:
        vol_needed = R['vol_normal']

    vol_spike = avg_vol_3d >= vol_20ma * vol_needed
    return vol_spike, round(avg_vol_3d / vol_20ma, 1), vol_needed

def check_not_extended(df, idx):
    close = df['Close'].iloc[idx]
    dma50 = df['Close'].rolling(50).mean().iloc[idx]
    if pd.isna(dma50): return False, 0
    extension_pct = (close / dma50 - 1) * 100
    not_extended = extension_pct <= R['extension']
    return not_extended, round(extension_pct, 1)

def add_indicators(df):
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    return df

# 5. MAIN BACKTEST - NO SKIP
def backtest_no_miss(df_daily, end_date, ticker):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 100: return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 63 # 3 mahine se start

    while i < len(df_daily) - 5:
        today = df_daily.iloc[i]

        rs_ok, rs_grade, stock_ret, nifty_ret, rs_ratio = check_relative_strength(df_daily, today.name)
        if not rs_ok:
            i += 1; continue

        not_ext, ext_pct = check_not_extended(df_daily, i)
        if not not_ext:
            i += 1; continue # 3 se 1 kiya - Har din check

        base_ok, breakout, near_high, base_high, base_low, base_pct, base_max_used = check_base_breakout(df_daily, i, rs_grade)
        if not base_ok or not breakout:
            i += 1; continue

        buyer_ok, vol_ratio, vol_needed = check_buyer_dominance(df_daily, i, rs_grade)
        if not buyer_ok:
            i += 1; continue

        entry_price = float(today['Close'])
        sl = float(base_low)
        risk = entry_price - sl
        if risk <= 0: i += 1; continue

        target = entry_price + (risk * 2.0)

        exit_price, exit_date, days, result = entry_price, today.name, 0, 'Running'
        for k in range(i + 1, len(df_daily)):
            days += 1
            h, l, c = df_daily['High'].iloc[k], df_daily['Low'].iloc[k], df_daily['Close'].iloc[k]
            if l <= sl:
                exit_price, exit_date, result = sl, df_daily.index[k], 'SL Hit'; break
            if h >= target:
                exit_price, exit_date, result = target, df_daily.index[k], 'Target Hit'; break
            if days > 60: # 40 se 60 - 2 mahine hold
                exit_price, exit_date, result = float(c), df_daily.index[k], 'Time Stop'; break
            if k == len(df_daily) - 1:
                exit_price, exit_date, result = float(c), df_daily.index[k], 'Running'

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        trades.append({
            'entry_date': today.name.strftime('%Y-%m-%d'),
            'rs_grade': rs_grade,
            'stock_ret': stock_ret,
            'nifty_ret': nifty_ret,
            'rs_ratio': rs_ratio,
            'ext_50dma': ext_pct,
            'base_pct': base_pct,
            'vol_x': vol_ratio,
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'exit_price': round(exit_price, 2),
            'days': int(days),
            'pl_pct': round(pl_pct, 2),
            'result': result
        })

        i += 1 # NO SKIP - Har din check
        continue

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks...")

for i, stock in enumerate(stocks):
    try:
        df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue

        trades = backtest_no_miss(df, ref_date, stock)
        if len(trades) == 0:
            continue

        for trade in trades:
            tag = f"🦸{trade['rs_grade']}"
            print(f" {stock} {trade['entry_date']} {tag} | RS:{trade['rs_ratio']}x | {trade['stock_ret']}% vs Nifty {trade['nifty_ret']}% | {trade['result']} {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade})
        time.sleep(0.1)
    except Exception as e:
        continue

# 7. OUTPUT
try:
    ws_output = sh.worksheet("RS_Base_Buyer_Final")
except:
    ws_output = sh.add_worksheet(title="RS_Base_Buyer_Final", rows=5000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('pl_pct', ascending=False) # Best hero upar

    def convert_to_native(val):
        if isinstance(val, (np.integer, np.int64)): return int(val)
        elif isinstance(val, (np.floating, np.float64)): return float(val)
        else: return val
    df_out = df_out.applymap(convert_to_native)

    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    total_trades = len(df_out)
    win_trades = (df_out['pl_pct'] > 0).sum()
    win_rate = round(win_trades / total_trades * 100, 1)
    total_pl = round(df_out['pl_pct'].sum(), 2)
    grade_stats = df_out.groupby('rs_grade')['pl_pct'].agg(['count', 'sum', 'mean']).round(2)

    summary = [
        ['', ''], ['TOTAL HERO', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['', ''], ['RS_GRADE', 'TRADES', 'TOTAL_P&L', 'AVG_P&L']
    ]
    for grade, row in grade_stats.iterrows():
        summary.append([grade, int(row['count']), float(row['sum']), float(row['mean'])])

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} HERO FOUND | {win_rate}% WIN ===")
    print("\nGRADE WISE:")
    print(grade_stats)
    print("\nTOP 10 HERO:")
    print(df_out[['Stock', 'entry_date', 'rs_grade', 'pl_pct']].head(10))
else:
    ws_output.update('A1', [["Ab bhi 0? Bhai market sach me dead hai ya sheet me stock hi nahi"]])
    print("\n=== AB BHI 0? SHEET CHECK KAR STOCK HAI BHI? ===")
