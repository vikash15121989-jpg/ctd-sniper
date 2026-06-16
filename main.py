import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== VA-PA SNIPER V1 - PRICE ACTION + VOLUME BACKTEST ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

BACKTEST_START = datetime(2023, 4, 1)
BACKTEST_END = datetime(2026, 5, 30)

print(f"Period: {BACKTEST_START.date()} to {BACKTEST_END.date()}", flush=True)

# ===== 2. STRATEGY RULES =====
R = {
    'base_min_days': 30, 'base_max_days': 60, 'base_range_max_pct': 15.0,
    'base_vol_dry_pct': 0.40,
    'bo_vol_spike': 2.5,
    'bo_buffer_pct': 0.5,
    'retest_vol_max_pct': 0.30,
    'retest_zone_pct': 3.0,
    'sl_buffer_pct': 1.0,
    'target_r': 2.0,
    'min_price': 50, 'min_daily_value_cr': 0.5,
    'max_risk_pct': 15.0, 'min_rr_pct': 8.0
}

def add_indicators(df):
    df['Vol_50MA'] = df['Volume'].rolling(50).mean()
    df['Daily_Value_20MA'] = (df['Close'] * df['Volume']).rolling(20).mean()
    df['50DMA'] = df['Close'].rolling(50).mean()
    df['200DMA'] = df['Close'].rolling(200).mean()
    df['Range_30D'] = (df['High'].rolling(30).max() - df['Low'].rolling(30).min()) / df['Low'].rolling(30).min() * 100
    return df

def check_liquidity(df, idx):
    try:
        if df['Close'].iloc[idx] < R['min_price']: return False
        if df['Daily_Value_20MA'].iloc[idx] < R['min_daily_value_cr'] * 1e7: return False
        return True
    except: return False

def find_base_and_breakout(df, idx):
    if idx < 100: return False, {}
    row = df.iloc[idx]

    if row['50DMA'] < row['200DMA'] or row['Close'] < row['50DMA']:
        return False, {}

    for base_days in range(R['base_min_days'], R['base_max_days'] + 1):
        if idx - base_days < 0: continue
        base_df = df.iloc[idx-base_days:idx]
        base_high = base_df['High'].max()
        base_low = base_df['Low'].min()
        base_range = (base_high - base_low) / base_low * 100
        if base_range > R['base_range_max_pct']: continue

        avg_base_vol = base_df['Volume'].mean()
        if avg_base_vol > row['Vol_50MA'] * R['base_vol_dry_pct']: continue

        bo_level = base_high * (1 + R['bo_buffer_pct']/100)
        if row['Close'] <= bo_level: continue
        if row['Close'] <= row['Open']: continue
        if row['Volume'] < row['Vol_50MA'] * R['bo_vol_spike']: continue

        return True, {
            'bo_date': df.index[idx].strftime('%Y-%m-%d'),
            'bo_level': round(base_high, 2),
            'bo_close': round(row['Close'], 2),
            'bo_vol': row['Volume'],
            'base_days': base_days,
            'base_range_pct': round(base_range, 1),
            'bo_idx': idx
        }
    return False, {}

def check_retest_entry(df, idx, bo_data):
    if idx <= bo_data['bo_idx'] + 1: return False, {}
    row = df.iloc[idx]
    bo_level = bo_data['bo_level']
    bo_vol = bo_data['bo_vol']

    zone_low = bo_level * (1 - R['retest_zone_pct']/100)
    zone_high = bo_level * (1 + R['retest_zone_pct']/100)
    if not (zone_low <= row['Low'] <= zone_high): return False, {}
    if row['Volume'] > bo_vol * R['retest_vol_max_pct']: return False, {}
    if row['Close'] < row['Open'] * 0.995: return False, {}

    swing_low = df['Low'].iloc[idx-5:idx+1].min()
    sl_price = swing_low * (1 - R['sl_buffer_pct']/100)
    risk = row['Close'] - sl_price
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['max_risk_pct'] or risk_pct <= 0: return False, {}

    target = row['Close'] + risk * R['target_r']
    target_pct = (target - row['Close']) / row['Close'] * 100
    if target_pct < R['min_rr_pct']: return False, {}

    return True, {
        'Entry_Date': df.index[idx].strftime('%Y-%m-%d'),
        'Entry': round(row['Close'], 2), 'SL': round(sl_price, 2),
        'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
        'Reward_%': round(target_pct, 1), 'RR': R['target_r'],
        'BO_Level': bo_level, 'BO_Date': bo_data['bo_date'],
        'Base_Days': bo_data['base_days'], 'Base_Range_%': bo_data['base_range_pct'],
        'BO_Vol_x': round(bo_vol / row['Vol_50MA'], 1),
        'Entry_Vol_%': round(row['Volume'] / bo_vol * 100, 1)
    }

def simulate_trade(df, entry_idx, sl, target):
    for i in range(entry_idx + 1, min(entry_idx + 60, len(df))):
        if df['Low'].iloc[i] <= sl:
            return 'LOSS', df.index[i].strftime('%Y-%m-%d'), round((sl / df['Close'].iloc[entry_idx] - 1) * 100, 1)
        if df['High'].iloc[i] >= target:
            return 'WIN', df.index[i].strftime('%Y-%m-%d'), round((target / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    exit_price = df['Close'].iloc[min(entry_idx + 59, len(df)-1)]
    pnl = round((exit_price / df['Close'].iloc[entry_idx] - 1) * 100, 1)
    return 'TIME', df.index[min(entry_idx + 59, len(df)-1)].strftime('%Y-%m-%d'), pnl

def scan_stock_vapa(stock):
    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=300),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200 or df['Close'].isna().all(): return []

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100: return []
        df = add_indicators(df)
        results = []
        last_bo_idx = -100

        for i in range(100, len(df)):
            if not check_liquidity(df, i): continue
            is_bo, bo_data = find_base_and_breakout(df, i)
            if is_bo and i > last_bo_idx + 20:
                last_bo_idx = i
                for j in range(i+1, min(i+21, len(df))):
                    is_entry, entry_data = check_retest_entry(df, j, bo_data)
                    if is_entry:
                        result, exit_date, pnl = simulate_trade(df, j, entry_data['SL'], entry_data['Target'])
                        entry_data.update({
                            'Stock': stock, 'Result': result,
                            'Exit_Date': exit_date, 'PnL_%': pnl
                        })
                        results.append(entry_data)
                        break
        return results
    except: return []

# ===== MAIN BACKTEST =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    all_results.extend(scan_stock_vapa(stock))
    if i % 50 == 0: print(f"Done {i}/{len(stocks)}", flush=True)

if not all_results:
    print("0 SETUP MILA")
    exit()

df_res = pd.DataFrame(all_results)
df_res = df_res.sort_values('Entry_Date')

total = len(df_res)
wins = len(df_res[df_res['Result'] == 'WIN'])
loss = len(df_res[df_res['Result'] == 'LOSS'])
winrate = round(wins / total * 100, 1) if total else 0
total_pnl = df_res['PnL_%'].sum()
avg_win = df_res[df_res['PnL_%'] > 0]['PnL_%'].mean()
avg_loss = df_res[df_res['PnL_%'] < 0]['PnL_%'].mean()

summary = pd.DataFrame([{
    'Total_Setups': total, 'Wins': wins, 'Loss': loss, 'Winrate_%': winrate,
    'Total_PnL_%': round(total_pnl, 1), 'Avg_Win_%': round(avg_win, 1),
    'Avg_Loss_%': round(avg_loss, 1), 'Avg_RR': R['target_r'],
    'Period': f"{BACKTEST_START.date()} to {BACKTEST_END.date()}",
    'Strategy': 'VA-PA SNIPER V1'
}])

def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=10000, cols=30)
        ws.clear()
    if not df.empty:
        payload = [df.columns.values.tolist()] + df.values.tolist()
        ws.update('A1', payload)
        return len(df)
    return 0

count_trades = update_gsheet('VAPA_BACKTEST_TRADES', df_res)
count_summary = update_gsheet('VAPA_BACKTEST_SUMMARY', summary)

print(f"\n=== VAPA BACKTEST COMPLETE ===", flush=True)
print(f"Total Setups: {total} | Winrate: {winrate}% | Net P&L: {total_pnl}%", flush=True)
print(f"Sheets: VAPA_BACKTEST_TRADES, VAPA_BACKTEST_SUMMARY", flush=True)
