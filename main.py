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

print("=== V10.3 RS + BASE + BUYER + NOT EXTENDED ===")

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

# 2. NIFTY CACHE
print("Loading Nifty...")
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)

# 3. CORE FUNCTIONS
def check_relative_strength(stock_df, check_date):
    """Rule 1: Stock Nifty se tez"""
    try:
        stock_6m = stock_df.loc[:check_date].iloc[-126:]['Close']
        nifty_6m = nifty_df.loc[:check_date].iloc[-126:]['Close']
        stock_ret = (stock_6m.iloc[-1] / stock_6m.iloc[0] - 1) * 100
        nifty_ret = (nifty_6m.iloc[-1] / nifty_6m.iloc[0] - 1) * 100
        if nifty_ret > 0:
            rs_ok = stock_ret > nifty_ret * 1.5
        else:
            rs_ok = stock_ret > nifty_ret + 10
        return rs_ok, round(stock_ret, 1), round(nifty_ret, 1)
    except:
        return False, 0, 0

def check_base_formation(df, idx):
    """Rule 2: Tight base 3-10%"""
    lookback = df.iloc[idx-60:idx]
    base_high, base_low = lookback['High'].max(), lookback['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100
    tight_base = 3 <= base_range_pct <= 10
    near_high = df['Close'].iloc[idx] >= base_high * 0.95
    return tight_base, near_high, base_high, base_low, round(base_range_pct, 1)

def check_buyer_dominance(df, idx):
    """Rule 3: Buyer havi"""
    today = df.iloc[idx]
    week_ago = df.iloc[idx-5]
    obv_rising = today['OBV'] > week_ago['OBV']
    obv_above_ma = today['OBV'] >= today['OBV_20MA'] * 0.98
    vol_spike = today['Volume'] >= today['Vol_20MA'] * 2.0
    buyer_present = obv_rising and obv_above_ma and vol_spike
    return buyer_present, round(today['Volume'] / today['Vol_20MA'], 1)

def check_not_extended(df, idx):
    """Rule 4: 50DMA se 20% se zyada door nahi"""
    close = df['Close'].iloc[idx]
    dma50 = df['Close'].rolling(50).mean().iloc[idx]
    if pd.isna(dma50): return False, 0
    extension_pct = (close / dma50 - 1) * 100
    not_extended = extension_pct <= 20 # 20% se kam
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

# 4. MAIN BACKTEST
def backtest_v10_3(df_daily, end_date, ticker):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 300: return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 126

    while i < len(df_daily) - 10:
        today = df_daily.iloc[i]

        # CHECK 1: RS
        rs_ok, stock_6m, nifty_6m = check_relative_strength(df_daily, today.name)
        if not rs_ok:
            i += 1; continue

        # CHECK 2: NOT EXTENDED - PEHLE YE CHECK KARO
        not_ext, ext_pct = check_not_extended(df_daily, i)
        if not not_ext:
            i += 5; continue # 5 din skip, extended stock me time waste nahi

        # CHECK 3: BASE
        base_ok, near_high, base_high, base_low, base_pct = check_base_formation(df_daily, i)
        if not base_ok or not near_high:
            i += 1; continue

        # CHECK 4: BUYER
        buyer_ok, vol_ratio = check_buyer_dominance(df_daily, i)
        if not buyer_ok:
            i += 1; continue

        # ========== ALL 4 CONDITIONS MET ==========
        entry_price = float(today['Close'])
        sl = float(base_low)
        risk = entry_price - sl
        if risk <= 0: i += 1; continue
        target = entry_price + (risk * 2.0)

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
            'stock_6m': stock_6m,
            'nifty_6m': nifty_6m,
            'ext_50dma': ext_pct,
            'base_pct': base_pct,
            'vol_x': vol_ratio,
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

        trades = backtest_v10_3(df, ref_date, stock)
        if len(trades) == 0:
            print("No Setup")
            continue

        for trade in trades:
            print(f" {trade['entry_date']} | RS:{trade['stock_6m']}% | Ext:{trade['ext_50dma']}% | Base:{trade['base_pct']}% | Vol:{trade['vol_x']}x | {trade['result']} | {trade['pl_pct']}%")
            signals.append({'Stock': stock, **trade})
        time.sleep(0.3)
    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. OUTPUT
try:
    ws_output = sh.worksheet("RS_Base_Buyer_Final")
except:
    ws_output = sh.add_worksheet(title="RS_Base_Buyer_Final", rows=2000, cols=15)

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
    avg_days = round(df_out['days'].mean(), 1)

    year_stats = df_out.groupby(pd.to_datetime(df_out['entry_date']).dt.year)['pl_pct'].agg(['count', 'sum', 'mean']).round(2)

    summary = [
        ['', ''], ['TOTAL TRADES', int(total_trades)], ['WIN RATE %', float(win_rate)],
        ['TOTAL P&L %', float(total_pl)], ['AVG P&L %', float(avg_pl)], ['AVG HOLD DAYS', float(avg_days)],
        ['', ''], ['YEAR', 'TRADES', 'TOTAL P&L', 'AVG P&L']
    ]
    for year, row in year_stats.iterrows():
        summary.append([int(year), int(row['count']), float(row['sum']), float(row['mean'])])

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} TRADES | {win_rate}% WIN | {total_pl}% TOTAL | {avg_pl}% AVG ===")
    print("\nYEAR WISE:")
    print(year_stats)
else:
    ws_output.update('A1', [["No Trades - All 4 Conditions Not Met"]])
    print("\n=== DONE: 0 TRADES ===")
