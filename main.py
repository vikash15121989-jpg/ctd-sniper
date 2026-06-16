import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER BACKTEST V16.6 SANE MODE ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# BACKTEST DATES
BACKTEST_START = datetime(2024, 10, 1)
BACKTEST_END = datetime(2026, 5, 30) # May 2026 tak

print(f"Backtest Period: {BACKTEST_START.date()} to {BACKTEST_END.date()}", flush=True)

# ===== 2. NIFTY DATA =====
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)
if nifty.empty or len(nifty) < 250:
    raise ValueError("Nifty data nahi mila")
nifty = nifty.dropna()

# ===== 3. SANE RULES - BEAR KE LIYE REALISTIC =====
R = {
    'min_price': 50, 'min_daily_value_cr': 0.5, 'min_vol_shares': 100000,
    'swing_lookback': 60, 'swing_pullback_min': 8.0, 'swing_pullback_max': 30.0,
    'swing_vol_dry_pct': 0.40, 'swing_sl_buffer': 0.12, 'swing_target_r': 1.5,
    'swing_min_rr_pct': 6.0, 'swing_max_risk_pct': 20.0, 'swing_min_drop_pct': 12.0,
    # 3 SANE FILTER
    'must_close_above_50dma': True,
    'must_have_rs_vs_nifty': 0.0,
    'vol_spike_choch': 1.2,
}

def add_indicators(df):
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['50DMA'] = df['Close'].rolling(50).mean()
    df['200DMA'] = df['Close'].rolling(200).mean()
    return df

def check_liquidity(df, idx):
    try:
        close = df['Close'].iloc[idx]
        vol_20ma = df['Vol_20MA'].iloc[idx]
        daily_val = df['Daily_Value_20MA'].iloc[idx]
        if pd.isna(close) or close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

def detect_basic_choch(df, idx):
    if idx < 60: return False, {}
    lookback = R['swing_lookback']
    recent = df.iloc[idx-lookback:idx]
    if len(recent) < 30: return False, {}

    high_lookback = recent['High'].max()
    low_lookback = recent['Low'].min()
    drop_pct = (high_lookback - low_lookback) / high_lookback * 100
    if drop_pct < R['swing_min_drop_pct']: return False, {}

    swing_highs = []
    for i in range(5, len(recent)-5):
        if recent['High'].iloc[i] == recent['High'].iloc[i-5:i+6].max():
            swing_highs.append(i)
    if len(swing_highs) < 1: return False, {}

    last_lh_idx = swing_highs[-1]
    last_lh_price = recent['High'].iloc[last_lh_idx]

    choch_idx = None
    for i in range(last_lh_idx + 1, len(recent)):
        if recent['Close'].iloc[i] > last_lh_price:
            choch_idx = i
            break
    if choch_idx is None: return False, {}

    return True, {
        'choch_date': recent.index[choch_idx].strftime('%Y-%m-%d'),
        'bos_level': round(last_lh_price, 2),
        'choch_idx': idx - lookback + choch_idx,
        'choch_high': round(recent.iloc[choch_idx:]['High'].max(), 2),
        'choch_vol': recent['Volume'].iloc[choch_idx:choch_idx+3].mean()
    }

def check_basic_pullback(df, idx, choch_data):
    bos_level = choch_data['bos_level']
    choch_high = choch_data['choch_high']
    choch_vol = choch_data['choch_vol']

    if idx <= choch_data['choch_idx'] + 2: return False, {}
    row = df.iloc[idx]

    pullback_pct = (choch_high - row['Low']) / choch_high * 100
    if not (R['swing_pullback_min'] <= pullback_pct <= R['swing_pullback_max']):
        return False, {}

    zone_low = bos_level * 0.90
    zone_high = bos_level * 1.10
    if not (zone_low <= row['Low'] <= zone_high):
        return False, {}

    if row['Volume'] > choch_vol * R['swing_vol_dry_pct']:
        return False, {}

    # ===== 3 SANE FILTER =====
    # 1. 50DMA reclaim
    if R['must_close_above_50dma'] and row['Close'] < row['50DMA']:
        return False, {}

    # 2. RS vs Nifty
    try:
        nifty_close_choch = nifty.loc[df.index[choch_data['choch_idx']], 'Close']
        nifty_close_now = nifty.loc[df.index[idx], 'Close']
        nifty_ret = (nifty_close_now / nifty_close_choch - 1) * 100
        stock_ret = (row['Close'] / df['Close'].iloc[choch_data['choch_idx']] - 1) * 100
        rs_score = stock_ret - nifty_ret
        if rs_score < R['must_have_rs_vs_nifty']: return False, {}
    except: return False, {}

    # 3. CHOCH pe volume spike
    if df['Volume'].iloc[choch_data['choch_idx']] < df['Vol_20MA'].iloc[choch_data['choch_idx']] * R['vol_spike_choch']:
        return False, {}

    # SL & Target
    swing_low = df['Low'].iloc[idx-10:idx+1].min()
    sl_price = swing_low * (1 - R['swing_sl_buffer'])

    risk = row['Close'] - sl_price
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['swing_max_risk_pct'] or risk_pct <= 0: return False, {}

    target = row['Close'] + risk * R['swing_target_r']
    target_pct = (target - row['Close']) / row['Close'] * 100
    if target_pct < R['swing_min_rr_pct']: return False, {}

    return True, {
        'Entry_Date': df.index[idx].strftime('%Y-%m-%d'),
        'Entry': round(row['Close'], 2), 'SL': round(sl_price, 2),
        'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
        'Reward_%': round(target_pct, 1), 'RR': round(target_pct / risk_pct, 1),
        'Pullback_%': round(pullback_pct, 1),
        'Vol_Dry_%': round(row['Volume'] / choch_vol * 100, 1),
        'RS_vs_Nifty_%': round(rs_score, 1),
        'BOS_Level': bos_level,
        'CHOCH_Date': choch_data['choch_date']
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

def scan_stock_backtest(stock):
    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=200),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100 or df['Close'].isna().all():
            return []

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 50: return []
        df = add_indicators(df)
        results = []

        for i in range(60, len(df)):
            if not check_liquidity(df, i): continue

            is_choch, choch_data = detect_basic_choch(df, i)
            if not is_choch: continue

            is_pb, pb_data = check_basic_pullback(df, i, choch_data)
            if not is_pb: continue

            result, exit_date, pnl = simulate_trade(df, i, pb_data['SL'], pb_data['Target'])
            pb_data.update({
                'Stock': stock, 'Result': result,
                'Exit_Date': exit_date, 'PnL_%': pnl
            })
            results.append(pb_data)
        return results
    except Exception as e:
        return []

# ===== MAIN BACKTEST =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks from CTD_Sniper Watchlist...", flush=True)

all_results = []
for i, stock in enumerate(stocks):
    all_results.extend(scan_stock_backtest(stock))
    if i % 50 == 0: print(f"Done {i}/{len(stocks)}", flush=True)

if not all_results:
    print("0 SETUP MILA - Is period me kuch nahi bana")
    exit()

df_res = pd.DataFrame(all_results)
df_res = df_res.sort_values('Entry_Date')

# Stats
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
    'Avg_Loss_%': round(avg_loss, 1),
    'Period': f"{BACKTEST_START.date()} to {BACKTEST_END.date()}"
}])

# ===== UPDATE GOOGLE SHEET =====
def update_gsheet(sheet_name, df):
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=10000, cols=25)
        ws.clear()

    if not df.empty:
        payload = [df.columns.values.tolist()] + df.values.tolist()
        ws.update('A1', payload)
        return len(df)
    else:
        ws.update('A1', [['No data']])
        return 0

count_trades = update_gsheet('BACKTEST_ALL_TRADES', df_res)
count_summary = update_gsheet('BACKTEST_SUMMARY', summary)

print(f"\n=== BACKTEST COMPLETE ===", flush=True)
print(f"Total Setups: {total} | Winrate: {winrate}% | Net P&L: {total_pnl}%", flush=True)
print(f"CTD_Sniper me 2 sheet bani:", flush=True)
print(f"1. BACKTEST_ALL_TRADES: {count_trades} trades", flush=True)
print(f"2. BACKTEST_SUMMARY: Summary", flush=True)
