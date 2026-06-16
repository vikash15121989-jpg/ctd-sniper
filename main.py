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

BACKTEST_START = datetime(2023, 4, 1) # BULL period se test kar
BACKTEST_END = datetime(2026, 5, 30)

print(f"Period: {BACKTEST_START.date()} to {BACKTEST_END.date()}", flush=True)

# ===== 2. STRATEGY RULES =====
R = {
    'base_min_days': 30, 'base_max_days': 60, 'base_range_max_pct': 15.0,
    'base_vol_dry_pct': 0.40, # Base me volume 40% se kam
    'bo_vol_spike': 2.5, # Breakout pe 2.5x volume
    'bo_buffer_pct': 0.5, # BO level + 0.5% cross
    'retest_vol_max_pct': 0.30, # Retest pe BO volume ka 30% max
    'retest_zone_pct': 3.0, # BO level ke 3% aas paas
    'sl_buffer_pct': 1.0, # SL = Low - 1%
    'target_r': 2.0, # 1:2 RR
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
    """RULE 1+2+3: Trend + Base + BO"""
    if idx < 100: return False, {}

    row = df.iloc[idx]

    # RULE 1: Trend Filter
    if row['50DMA'] < row['200DMA'] or row['Close'] < row['50DMA']:
        return False, {}

    # RULE 2: Base check - pichle 30-60 din
    for base_days in range(R['base_min_days'], R['base_max_days'] + 1):
        if idx - base_days < 0: continue

        base_df = df.iloc[idx-base_days:idx]
        base_high = base_df['High'].max()
        base_low = base_df['Low'].min()
        base_range = (base_high - base_low) / base_low * 100

        if base_range > R['base_range_max_pct']: continue # Range tight nahi

        # Volume dry in base
        avg_base_vol = base_df['Volume'].mean()
        if avg_base_vol > row['Vol_50MA'] * R['base_vol_dry_pct']: continue

        # RULE 3: Breakout check - aaj ka candle
        bo_level = base_high * (1 + R['bo_buffer_pct']/100)
        if row['Close'] <= bo_level: continue # BO nahi hua
        if row['Close'] <= row['Open']: continue # Red candle nahi
        if row['Volume'] < row['Vol_50MA'] * R['bo_vol_spike']: continue # Volume nahi

        # BO confirm hua
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
    """RULE 4: Retest pe entry"""
    if idx <= bo_data['bo_idx'] + 1: return False, {}

    row = df.iloc[idx]
    bo_level = bo_data['bo_level']
    bo_vol = bo_data['bo_vol']

    # Retest zone me hai?
    zone_low = bo_level * (1 - R['retest_zone_pct']/100)
    zone_high = bo_level * (1 + R['retest_zone_pct']/100)
    if not (zone_low <= row['Low'] <= zone_high): return False, {}

    # Retest pe volume dry?
    if row['Volume'] > bo_vol * R['retest_vol_max_pct']: return False, {}

    # Green candle ya Doji - selling khatam
    if row['Close'] < row['Open'] * 0.995: return False, {} # Badi red nahi

    # SL & Target
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
        last_bo_idx = -100 # Ek BO ke baad 100 din wait

        for i in range(100, len(df)):
            if not check_liquidity(df, i): continue

            # Step 1: BO dhoondo
            is_bo, bo_data = find_base_and_breakout(df, i)
            if is_bo and
