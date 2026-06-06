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

print("=== V10.3I RS KILLER - AB 100% HERO ===", flush=True)

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

print(f"A1 Date: {ref_date.date()}", flush=True)

# 2. NIFTY CACHE
print("Downloading Nifty...", flush=True)
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)
print(f"Nifty Done. Last Trading Date: {nifty_df.index[-1].date()}", flush=True)

if ref_date.date() > nifty_df.index[-1].date():
    ref_date = nifty_df.index[-1].to_pydatetime()
    print(f"Date fix, {ref_date.date()} use kar raha", flush=True)

print(f"Final Scan Till: {ref_date.date()}", flush=True)

# 3. RULES - RS BILKUL LOOSE
R = {
    'rs_normal': 0.5, # Nifty se aadha bhi chalega
    'rs_hero': 0.8, # Nifty se 20% kam bhi HERO
    'rs_god': 1.5, # Nifty se 50% tez = GOD
    'extension': 100, # 60 se 100 - Kitna bhi door
    'base_min': 0, # 1 se 0 - No base bhi chalega
    'base_max_normal': 50, 'base_max_hero': 50, 'base_max_god': 50,
    'vol_normal': 0.5, 'vol_hero': 0.5, 'vol_god': 0.5, # Avg se aadha bhi chalega
}

fail_log = {'RS_Fail': 0, 'Ext_Fail': 0, 'Base_Fail': 0, 'Vol_Fail': 0, 'Data_Fail': 0}

# 4. FIXED RS - DATE ALIGN KIYA
def check_relative_strength(stock_df, check_date):
    try:
        periods = {'1M': 21, '3M': 63, '6M': 126}
        best_rs = 0
        best_stock_ret = 0
        best_nifty_ret = 0

        for period_name, days in periods.items():
            # FIX: Common dates nikalo dono me
            stock_window = stock_df.loc[:check_date].iloc[-days:].copy()
            nifty_window = nifty_df.loc[:check_date].iloc[-days:].copy()

            # Dono ko same dates pe align karo
            common_dates = stock_window.index.intersection(nifty_window.index)
            if len(common_dates) < 15: continue # 15 din minimum

            stock_window = stock_window.loc[common_dates]
            nifty_window = nifty_window.loc[common_dates]

            stock_ret = (stock_window['Close'].iloc[-1] / stock_window['Close'].iloc[0] - 1) * 100
            nifty_ret = (nifty_window['Close'].iloc[-1] / nifty_window['Close'].iloc[0] - 1) * 100

            if nifty_ret <= -50: # Nifty -50% crash me
                rs = 10 if stock_ret > -20 else 0 # Stock -20% se upar = GOD
            elif nifty_ret <= 0:
                rs = (stock_ret - nifty_ret) / 5 if stock_ret > nifty_ret else 0
            else:
                rs = stock_ret / nifty_ret if nifty_ret!= 0 else 0

            if rs > best_rs:
                best_rs = rs
                best_stock_ret = stock_ret
                best_nifty_ret = nifty_ret

        # RS Grade - ULTRA LOOSE
        if best_rs >= R['rs_god'] or best_stock_ret > 5:
            grade = 'GOD'; rs_ok = True
        elif best_rs >= R['rs_hero'] or best_stock_ret > 0:
            grade = 'HERO'; rs_ok = True
        elif best_rs >= R['rs_normal'] or best_stock_ret > -10:
            grade = 'NORMAL'; rs_ok = True
        else:
            grade = 'WEAK'; rs_ok = False

        return rs_ok, grade, round(best_stock_ret, 1), round(best_nifty_ret, 1), round(best_rs, 2)
    except Exception as e:
        return False, 'WEAK', 0, 0, 0

def check_base_breakout(df, idx, rs_grade):
    window = df.iloc[max(0, idx-9):idx+1] # 10 din window
    lookback = df.iloc[max(0, idx-60):idx]
    if len(lookback) < 10: return True, True, 0, 0, 0, 0 # Data kam = Pass kar do

    base_high, base_low = lookback['High'].max(), lookback['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100 if base_low > 0 else 0

    if rs_grade == 'GOD': base_max = R['base_max_god']
    elif rs_grade == 'HERO': base_max = R['base_max_hero']
    else: base_max = R['base_max_normal']

    tight_base = base_range_pct <= base_max # Min check hata diya
    breakout = (window['Close'] > base_high * 0.90).any() # 10% neeche bhi chalega
    near_high = True # Hamesha pass

    return tight_base, breakout, near_high, base_high, base_low, round(base_range_pct, 1), base_max

def check_buyer_dominance(df, idx, rs_grade):
    window = df.iloc[max(0, idx-9):idx+1] # 10 din avg
    avg_vol = window['Volume'].mean()
    vol_20ma = df['Vol_20MA'].iloc[idx]

    if rs_grade == 'GOD': vol_needed = R['vol_god']
    elif rs_grade == 'HERO': vol_needed = R['vol_hero']
    else: vol_needed = R['vol_normal']

    vol_spike = avg_vol >= vol_20ma * vol_needed
    return vol_spike, round(avg_vol / vol_20ma, 1), vol_needed

def check_not_extended(df, idx):
    return True, 0 # Extension check hata diya - Kitna bhi door chalega

def add_indicators(df):
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    return df

def backtest_final(df_daily, end_date, ticker):
    global fail_log
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 30:
        fail_log['Data_Fail'] += 1
        return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 30 # 30 din se start

    while i < len(df_daily) - 5:
        today = df_daily.iloc[i]

        rs_ok, rs_grade, stock_ret, nifty_ret, rs_ratio = check_relative_strength(df_daily, today.name)
        if not rs_ok:
            fail_log['RS_Fail'] += 1
            i += 5; continue

        not_ext, ext_pct = check_not_extended(df_daily, i)
        if not not_ext:
            fail_log['Ext_Fail'] += 1
            i += 5; continue

        base_ok, breakout, near_high, base_high, base_low, base_pct, base_max_used = check_base_breakout(df_daily, i, rs_grade)
        if not base_ok or not breakout:
            fail_log['Base_Fail'] += 1
            i += 5; continue

        buyer_ok, vol_ratio, vol_needed = check_buyer_dominance(df_daily, i, rs_grade)
        if not buyer_ok:
            fail_log['Vol_Fail'] += 1
            i += 5; continue

        entry_price = float(today['Close'])
        sl = float(base_low) if base_low > 0 else entry_price * 0.9
        risk = entry_price - sl
        if risk <= 0: i += 5; continue

        target = entry_price + (risk * 2.0)

        exit_price, exit_date, days, result = entry_price, today.name, 0, 'Running'
        for k in range(i + 1, min(i + 90, len(df_daily))):
            days += 1
            h, l, c = df_daily['High'].iloc[k], df_daily['Low'].iloc[k], df_daily['Close'].iloc[k]
            if l <= sl:
                exit_price, exit_date, result = sl, df_daily.index[k], 'SL Hit'; break
            if h >= target:
                exit_price, exit_date, result = target, df_daily.index[k], 'Target Hit'; break
            if days > 90:
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

        i = k + 5
        continue

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 30:
            fail_log['Data_Fail'] += 1
            continue

        trades = backtest_final(df, ref_date, stock)
        if len(trades) == 0:
            continue

        for trade in trades:
            tag = f"🦸{trade['rs_grade']}"
            print(f" {stock} {trade['entry_date']} {tag} | RS:{trade['rs_ratio']}x | {trade['stock_ret']}% vs Nifty {trade['nifty_ret']}% | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append({'Stock': stock, **trade})
        time.sleep(0.2)
    except Exception as e:
        if i % 100 == 0:
            print(f"Error {stock}: {str(e)[:60]}", flush=True)
        continue

print(f"Scan Complete. Total Hero: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("RS_Base_Buyer_Final")
except:
    ws_output = sh.add_worksheet(title="RS_Base_Buyer_Final", rows=5000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('pl_pct', ascending=False)

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
    print(f"\n=== DONE: {total_trades} HERO FOUND | {win_rate}% WIN ===", flush=True)
    print("\nGRADE WISE:", flush=True)
    print(grade_stats, flush=True)
    print("\nTOP 10 HERO:", flush=True)
    print(df_out[['Stock', 'entry_date', 'rs_grade', 'pl_pct']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Hero Found - Check Fail Log"]])
    print("\n=== DONE: 0 HERO ===", flush=True)
    print(f"Fail Reasons: {fail_log}", flush=True)
