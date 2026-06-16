import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
warnings.filterwarnings('ignore')

print("=== CTD SNIPER V16.4 PROOF - BACKTEST TO GSHEET ===", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# BACKTEST DATES - Yaha change kar
BACKTEST_START = datetime(2024, 10, 1)
BACKTEST_END = datetime(2026, 5, 30)

print(f"Backtest Period: {BACKTEST_START.date()} to {BACKTEST_END.date()}", flush=True)

# ===== 2. NIFTY DATA FOR REGIME =====
nifty = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)
if nifty.empty or len(nifty) < 250:
    raise ValueError("Nifty data nahi mila")

nifty['200DMA'] = nifty['Close'].rolling(200).mean()
nifty['50DMA'] = nifty['Close'].rolling(50).mean()
nifty = nifty.dropna()

def detect_regime(date):
    df = nifty[nifty.index <= date].tail(250)
    if len(df) < 200: return "BEAR"
    close = float(df['Close'].iloc[-1])
    dma200 = float(df['200DMA'].iloc[-1])
    dma50 = float(df['50DMA'].iloc[-1])
    if close > dma200 and dma50 > dma200: return "BULL"
    return "BEAR"

# ===== 3. PROOF RULES =====
def get_rules(regime):
    if regime == "BULL":
        return {
            'min_price': 100, 'min_daily_value_cr': 2.0, 'min_vol_shares': 300000,
            'swing_lookback': 90, 'swing_pullback_min': 12.0, 'swing_pullback_max': 28.0,
            'swing_vol_dry_pct': 0.20, 'swing_sl_buffer': 0.10, 'swing_target_r': 2.5,
            'swing_min_rr_pct': 12.0, 'swing_max_risk_pct': 15.0, 'swing_min_drop_pct': 20.0,
            'choch_zone_pct': 5.0, 'rs_vs_nifty': 5.0, 'vol_spike_choch': 1.5,
        }
    else: # BEAR - PROOF MODE
        return {
            'min_price': 100, 'min_daily_value_cr': 1.0, 'min_vol_shares': 200000,
            'swing_lookback': 60, 'swing_pullback_min': 10.0, 'swing_pullback_max': 25.0,
            'swing_vol_dry_pct': 0.25, 'swing_sl_buffer': 0.12, 'swing_target_r': 2.0,
            'swing_min_rr_pct': 10.0, 'swing_max_risk_pct': 18.0, 'swing_min_drop_pct': 15.0,
            'choch_zone_pct': 7.0, 'rs_vs_nifty': 10.0, 'vol_spike_choch': 2.0,
        }

def add_indicators(df):
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['50DMA'] = df['Close'].rolling(50).mean()
    df['200DMA'] = df['Close'].rolling(200).mean()
    df['Range'] = df['High'] - df['Low']
    df_weekly = df.resample('W-FRI').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'})
    df_weekly['50WMA'] = df_weekly['Close'].rolling(50).mean()
    df['50WMA'] = df_weekly['50WMA'].reindex(df.index, method='ffill')
    return df

def check_liquidity(df, idx, R):
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

def detect_proof_choch(df, idx, R):
    if idx < 120: return False, {}
    lookback = R['swing_lookback']
    recent = df.iloc[idx-lookback:idx]
    if len(recent) < 50: return False, {}

    high_90d = recent['High'].max()
    low_90d = recent['Low'].min()
    drop_pct = (high_90d - low_90d) / high_90d * 100
    if drop_pct < R['swing_min_drop_pct']: return False, {}

    swing_highs = []
    for i in range(7, len(recent)-7):
        if recent['High'].iloc[i] == recent['High'].iloc[i-7:i+8].max():
            swing_highs.append(i)
    if len(swing_highs) < 1: return False, {}

    last_lh_idx = swing_highs[-1]
    last_lh_price = recent['High'].iloc[last_lh_idx]

    choch_idx = None
    for i in range(last_lh_idx + 3, len(recent)):
        if recent['Close'].iloc[i] > last_lh_price * 1.015:
            vol_avg_20 = recent['Vol_20MA'].iloc[i]
            if pd.isna(vol_avg_20): continue
            if recent['Volume'].iloc[i] > vol_avg_20 * R['vol_spike_choch']:
                choch_idx = i
                break
    if choch_idx is None: return False, {}

    after_choch_high = recent.iloc[choch_idx:]['High'].max()
    if after_choch_high < last_lh_price * 1.08: return False, {}

    return True, {
        'choch_date': recent.index[choch_idx].strftime('%Y-%m-%d'),
        'bos_level': round(last_lh_price, 2),
        'choch_idx': idx - lookback + choch_idx,
        'choch_high': round(after_choch_high, 2),
        'choch_vol': recent['Volume'].iloc[choch_idx:choch_idx+5].mean(),
        'choch_day_vol_spike': round(recent['Volume'].iloc[choch_idx] / recent['Vol_20MA'].iloc[choch_idx], 1)
    }

def check_proof_pullback(df, idx, choch_data, R):
    bos_level = choch_data['bos_level']
    choch_high = choch_data['choch_high']
    choch_vol = choch_data['choch_vol']

    if idx <= choch_data['choch_idx'] + 5: return False, {}
    row = df.iloc[idx]

    pullback_pct = (choch_high - row['Low']) / choch_high * 100
    if not (R['swing_pullback_min'] <= pullback_pct <= R['swing_pullback_max']):
        return False, {}

    low_since_choch = df['Low'].iloc[choch_data['choch_idx']:idx+1].min()
    idx_low = df['Low'].iloc[choch_data['choch_idx']:idx+1].idxmin()
    dma50_at_low = df.loc[idx_low, '50DMA']
    if pd.isna(dma50_at_low) or low_since_choch > dma50_at_low * 0.98: return False, {}
    if row['Close'] < row['50DMA']: return False, {}

    if row['Volume'] > choch_vol * R['swing_vol_dry_pct']: return False, {}

    try:
        nifty_close_choch = nifty.loc[df.index[choch_data['choch_idx']], 'Close']
        nifty_close_now = nifty.loc[df.index[idx], 'Close']
        nifty_ret = (nifty_close_now / nifty_close_choch - 1) * 100
        stock_ret = (row['Close'] / df['Close'].iloc[choch_data['choch_idx']] - 1) * 100
        rs_score = stock_ret - nifty_ret
        if rs_score < R['rs_vs_nifty']: return False, {}
    except: return False, {}

    if row['Close'] < row['Open']: return False, {}
    if idx > 0 and row['Low'] <= df['Low'].iloc[idx-1]: return False, {}

    swing_low = df['Low'].iloc[idx-15:idx+1].min()
    sl_price = swing_low * (1 - R['swing_sl_buffer'])
    sl_200dma = df['200DMA'].iloc[idx] * 0.98
    final_sl = min(sl_price, sl_200dma)

    risk = row['Close'] - final_sl
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['swing_max_risk_pct'] or risk_pct <= 0: return False, {}

    target = row['Close'] + risk * R['swing_target_r']
    target_pct = (target - row['Close']) / row['Close'] * 100
    if target_pct < R['swing_min_rr_pct']: return False, {}

    return True, {
        'Entry_Date': df.index[idx].strftime('%Y-%m-%d'),
        'Entry': round(row['Close'], 2), 'SL': round(final_sl, 2),
        'Target': round(target, 2), 'Risk_%': round(risk_pct, 1),
        'Reward_%': round(target_pct, 1), 'RR': round(target_pct / risk_pct, 1),
        'Pullback_%': round(pullback_pct, 1),
        'Vol_Dry_%': round(row['Volume'] / choch_vol * 100, 1),
        'CHOCH_Vol_Spike': choch_data['choch_day_vol_spike'],
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
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400),
                        end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 300 or df['Close'].isna().all():
            return []

        df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
        if len(df) < 100: return []
        df = add_indicators(df)
        results = []

        for i in range(120, len(df)):
            date = df.index[i]
            regime = detect_regime(date)
            R = get_rules(regime)

            if not check_liquidity(df, i, R): continue

            is_choch, choch_data = detect_proof_choch(df, i, R)
            if not is_choch: continue

            is_pb, pb_data = check_proof_pullback(df, i, choch_data, R)
            if not is_pb: continue

            result, exit_date, pnl = simulate_trade(df, i, pb_data['SL'], pb_data['Target'])
            pb_data.update({
                'Stock': stock, 'Regime': regime, 'Result': result,
                'Exit_Date': exit_date, 'PnL_%': pnl
            })
            results.append(pb_data)
        return results
    except Exception as e:
        print(f"Error {stock}: {e}")
        return []

# ===== MAIN BACKTEST =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
print(f"Scanning {len(stocks)} stocks from CTD_Sniper Watchlist...", flush=True)

all_results = []
with ProcessPoolExecutor(max_workers=8) as executor:
    futures = [executor.submit(scan_stock_backtest, stock) for stock in stocks]
    for i, future in enumerate(as_completed(futures)):
        all_results.extend(future.result())
        if i % 50 == 0: print(f"Done {i}/{len(stocks)}", flush=True)

if not all_results:
    print("0 SETUP MILA - Filter tight ya market khatam")
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
        ws = sh.add_worksheet(title=sheet_name, rows=5000, cols=25)
        ws.clear()

    if not df.empty:
        payload = [df.columns.values.tolist()] + df.values.tolist()
        ws.update('A1', payload)
        return len(df)
    else:
        ws.update('A1', [['No data']])
        return 0

count_trades = update_gsheet('PROOF_BACKTEST_TRADES', df_res)
count_summary = update_gsheet('PROOF_BACKTEST_SUMMARY', summary)

print(f"\n=== BACKTEST COMPLETE ===", flush=True)
print(f"Total Setups: {total} | Winrate: {winrate}% | Net P&L: {total_pnl}%", flush=True)
print(f"CTD_Sniper sheet me 2 nayi sheet bani:", flush=True)
print(f"1. PROOF_BACKTEST_TRADES: {count_trades} trades", flush=True)
print(f"2. PROOF_BACKTEST_SUMMARY: Summary stats", flush=True)
