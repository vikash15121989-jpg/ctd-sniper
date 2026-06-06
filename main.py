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

print("=== V10.3B PURE WAR HERO - NO MARKET BIAS ===")

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

print(f"Backtest Till: {ref_date.date()}")

# 2. NIFTY CACHE - Sirf RS ke liye
print("Loading Nifty...")
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)

# 3. WAR HERO RULES - MARKET NEUTRAL
R = {
    'rs_normal': 1.5, # Normal RS
    'rs_hero': 5.0, # Hero level RS
    'rs_god': 10.0, # God level RS
    'extension': 25, # 25% tak sabke liye
    'base_min': 3,
    'base_max_normal': 10, # Normal stock
    'base_max_hero': 8, # Hero ho to 8% tak
    'base_max_god': 12, # God ho to 12% bhi chalega
    'vol_normal': 2.0, # Normal 2x
    'vol_hero': 2.2, # Hero 2.2x
    'vol_god': 1.8, # God ho to 1.8x bhi chalega
}

# 4. CORE FUNCTIONS - STOCK DRIVEN LOGIC
def check_relative_strength(stock_df, check_date):
    """Rule 1: RS Grade Nikalo - Normal/Hero/God"""
    try:
        stock_6m = stock_df.loc[:check_date].iloc[-126:]['Close']
        nifty_6m = nifty_df.loc[:check_date].iloc[-126:]['Close']
        stock_ret = (stock_6m.iloc[-1] / stock_6m.iloc[0] - 1) * 100
        nifty_ret = (nifty_6m.iloc[-1] / nifty_6m.iloc[0] - 1) * 100

        if nifty_ret == 0:
            rs = 999 if stock_ret > 0 else 0
        else:
            rs = stock_ret / nifty_ret if nifty_ret > 0 else abs(stock_ret - nifty_ret) / 10

        # RS Grade
        if rs >= R['rs_god'] or (stock_ret > 30 and nifty_ret < 0):
            grade = 'GOD'
            rs_ok = True
        elif rs >= R['rs_hero'] or (stock_ret > 15 and nifty_ret < 0):
            grade = 'HERO'
            rs_ok = True
        elif rs >= R['rs_normal']:
            grade = 'NORMAL'
            rs_ok = True
        else:
            grade = 'WEAK'
            rs_ok = False

        return rs_ok, grade, round(stock_ret, 1), round(nifty_ret, 1), round(rs, 2)
    except:
        return False, 'WEAK', 0, 0, 0

def check_base_formation(df, idx, rs_grade):
    """Rule 2: Base - RS Grade ke hisab se"""
    lookback = df.iloc[idx-60:idx]
    base_high, base_low = lookback['High'].max(), lookback['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100

    # Dynamic Base Max - Stock ki taakat se
    if rs_grade == 'GOD':
        base_max = R['base_max_god'] # 12%
    elif rs_grade == 'HERO':
        base_max = R['base_max_hero'] # 8%
    else:
        base_max = R['base_max_normal'] # 10%

    tight_base = R['base_min'] <= base_range_pct <= base_max
    near_high = df['Close'].iloc[idx] >= base_high * 0.95
    return tight_base, near_high, base_high, base_low, round(base_range_pct, 1), base_max

def check_buyer_dominance(df, idx, rs_grade):
    """Rule 3: Volume - RS Grade ke hisab se"""
    today = df.iloc[idx]
    week_ago = df.iloc[idx-5]
    obv_rising = today['OBV'] > week_ago['OBV']
    obv_above_ma = today['OBV'] >= today['OBV_20MA'] * 0.98

    # Dynamic Volume - Stock ki taakat se
    if rs_grade == 'GOD':
        vol_needed = R['vol_god'] # 1.8x
    elif rs_grade == 'HERO':
        vol_needed = R['vol_hero'] # 2.2x
    else:
        vol_needed = R['vol_normal'] # 2.0x

    vol_spike = today['Volume'] >= today['Vol_20MA'] * vol_needed
    buyer_present = obv_rising and obv_above_ma and vol_spike
    return buyer_present, round(today['Volume'] / today['Vol_20MA'], 1), vol_needed

def check_not_extended(df, idx):
    """Rule 4: 25% extension sabke liye"""
    close = df['Close'].iloc[idx]
    dma50 = df['Close'].rolling(50).mean().iloc[idx]
    if pd.isna(dma50): return False, 0
    extension_pct = (close / dma50 - 1) * 100
    not_extended = extension_pct <= R['extension']
    return not_extended, round(extension_pct, 1)

def add_indicators(df):
    obv = [0]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            obv.append(obv[-1] + df['Volume'].iloc[i])
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            obv.append(obv[-1] - df['Volume'].iloc[i])
        else:
            obv.append(obv[-1])
    df['OBV'] = obv
    df['OBV_20MA'] = df['OBV'].rolling(20).mean()
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    return df

# 5. MAIN BACKTEST - PURE WAR HERO
def backtest_war_hero(df_daily, end_date, ticker):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 300: return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 126

    while i < len(df_daily) - 10:
        today = df_daily.iloc[i]

        # CHECK 1: RS GRADE
        rs_ok, rs_grade, stock_6m, nifty_6m, rs_ratio = check_relative_strength(df_daily, today.name)
        if not rs_ok:
            i += 1; continue

        # CHECK 2: NOT EXTENDED
        not_ext, ext_pct = check_not_extended(df_daily, i)
        if not not_ext:
            i += 5; continue

        # CHECK 3: BASE - RS GRADE SE
        base_ok, near_high, base_high, base_low, base_pct, base_max_used = check_base_formation(df_daily, i, rs_grade)
        if not base_ok or not near_high:
            i += 1; continue

        # CHECK 4: BUYER - RS GRADE SE
        buyer_ok, vol_ratio, vol_needed = check_buyer_dominance(df_daily, i, rs_grade)
        if not buyer_ok:
            i += 1; continue

        # ========== ALL 4 CONDITIONS MET ==========
        entry_price = float(today['Close'])
        sl = float(base_low)
        risk = entry_price - sl
        if risk <= 0: i += 1; continue

        # RISK/RR FIXED - MARKET NAHI DEKHTE
        target = entry_price + (risk * 2.0) # Hamesha 1:2
        risk_pct = 0.05 # Hamesha 5% risk. Position size se control karna.

        # BACKTEST
        exit_price, exit_date, days, result = entry_price, today.name, 0, 'Running'
        for k in range(i + 1, len(df_daily)):
            days += 1
            h, l, c = df_daily['High'].iloc[k], df_daily['Low'].iloc[k], df_daily['Close'].iloc[k]
            if l <= sl:
                exit_price, exit_date, result = sl, df_daily.index[k], 'SL Hit'; break
            if h >= target:
                exit_price, exit_date, result = target, df_daily.index[k], 'Target Hit'; break
            if days > 30:
                exit_price, exit_date, result = float(c), df_daily.index[k], 'Time Stop'; break
            if k == len(df_daily) - 1:
                exit_price, exit_date, result = float(c), df_daily.index[k], 'Running'

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        trades.append({
            'entry_date': today.name.strftime('%Y-%m-%d'),
            'rs_grade': rs_grade,
            'stock_6m': stock_6m,
            'nifty_6m': nifty_6m,
            'rs_ratio': rs_ratio,
            'ext_50dma': ext_pct,
            'base_pct': base_pct,
            'base_max': base_max_used,
            'vol_x': vol_ratio,
            'vol_needed': vol_needed,
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'exit_date': exit_date.strftime('%Y-%m-%d'),
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

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 300: continue

        trades = backtest_war_hero(df, ref_date, stock)
        if len(trades) == 0:
            print("No Setup")
            continue

        for trade in trades:
            tag = f"🦸{trade['rs_grade']}"
            print(f" {trade['entry_date']} {tag} | RS:{trade['rs_ratio']}x | Ext:{trade['ext_50dma']}% | Base:{trade['base_pct']}%/{trade['base_max']}% | Vol:{trade['vol_x']}x/{trade['vol_needed']}x | {trade['result']} | {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade})
        time.sleep(0.3)
    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. OUTPUT
try:
    ws_output = sh.worksheet("RS_Base_Buyer_Final")
except:
    ws_output = sh.add_worksheet(title="RS_Base_Buyer_Final", rows=2000, cols=20)

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
    win_trades = (df_out['pl_pct'] > 0).sum()
    win_rate = round(win_trades / total_trades * 100, 1)
    total_pl = round(df_out['pl_pct'].sum(), 2)
    avg_pl = round(df_out['pl_pct'].mean(), 2)

    grade_stats = df_out.groupby('rs_grade')['pl_pct'].agg(['count', 'sum', 'mean']).round(2)

    summary = [
        ['', ''], ['TOTAL TRADES', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG P&L %', float(avg_pl)], ['', ''],
        ['RS_GRADE', 'TRADES', 'TOTAL_P&L', 'AVG_P&L']
    ]
    for grade, row in grade_stats.iterrows():
        summary.append([grade, int(row['count']), float(row['sum']), float(row['mean'])])

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} TRADES | {win_rate}% WIN | {total_pl}% TOTAL ===")
    print("\nRS GRADE WISE:")
    print(grade_stats)
else:
    ws_output.update('A1', [["No Trades - Hero Not Found"]])
    print("\n=== DONE: 0 TRADES - CASH IS KING ===")
