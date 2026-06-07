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

print("=== V15.5 YEAR WISE PERFECT BACKTEST ===", flush=True)

# 1. SETUP
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. PERFECT RULES
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'min_vol_shares': 100000,
    'uptrend_days': 10,
    'vol_ma_days': 20,
    'scan_window': 60,
    'watchlist_days': 10,
    'target_pct': 15,
    'sl_pct': 8,
    'hold_days': 30,
    'checks_per_stock': 999,
    'gap_between_checks': 1,
    'min_vol_ratio': 1.0,
    'min_pullback_pct': 10.0,
    'max_52w_distance': 0.0,
}

def add_indicators(df):
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['New_High_10D'] = df['High'] > df['High'].shift(1).rolling(10).max()
    return df

def check_liquidity(df):
    try:
        close = df['Close'].iloc[-1]
        vol_20ma = df['Vol_20MA'].iloc[-1]
        daily_val = df['Daily_Value_20MA'].iloc[-1]
        if pd.isna(close) or close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

def find_perfect_pullback_silent(df, end_idx):
    try:
        start_idx = max(0, end_idx - R['scan_window'] + 1)
        for i in range(start_idx, end_idx + 1):
            if i < 252: continue
            uptrend_start = i - R['uptrend_days']
            if uptrend_start < 0: continue
            uptrend_zone = df.iloc[uptrend_start:i]
            new_highs = uptrend_zone['New_High_10D'].sum()
            avg_vol = uptrend_zone['Volume'].mean()
            vol_20ma = df['Vol_20MA'].iloc[i]
            if pd.isna(vol_20ma): continue
            uptrend = new_highs >= 3 and avg_vol > vol_20ma
            if not uptrend: continue

            row = df.iloc[i]
            vol_max_10d = row['Vol_10D_Max']
            high_max_10d = row['High_10D_Max']
            if pd.isna(vol_max_10d) or pd.isna(high_max_10d): continue

            pullback_depth = ((high_max_10d - row['Low']) / high_max_10d) * 100
            if pullback_depth < R['min_pullback_pct']: continue

            pullback_cond1 = row['High'] < high_max_10d
            pullback_cond2 = row['Volume'] < vol_max_10d
            if not (pullback_cond1 and pullback_cond2): continue

            pullback_idx = i
            search_end = min(pullback_idx + 1 + R['watchlist_days'], len(df))

            for j in range(pullback_idx + 1, search_end):
                silent_row = df.iloc[j]
                vol_max_10d_silent = silent_row['Vol_10D_Max']
                high_max_10d_silent = silent_row['High_10D_Max']
                if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent): continue

                vol_ratio = silent_row['Volume'] / vol_max_10d_silent
                if vol_ratio < R['min_vol_ratio']: continue

                silent_cond1 = silent_row['Volume'] > vol_max_10d_silent
                silent_cond2 = silent_row['High'] < high_max_10d_silent
                if silent_cond1 and silent_cond2:
                    entry_price = high_max_10d_silent
                    year_high = df['High'].iloc[max(0, j-252):j].max()
                    nearness_52w = ((year_high - entry_price) / year_high) * 100
                    if nearness_52w > R['max_52w_distance']: continue

                    details = {
                        'uptrend_start_date': df.index[uptrend_start].strftime('%Y-%m-%d'),
                        'pullback_date': df.index[pullback_idx].strftime('%Y-%m-%d'),
                        'watchlist_date': df.index[j].strftime('%Y-%m-%d'),
                        'entry_price': round(entry_price, 2),
                        'silent_vol_ratio': round(vol_ratio, 2),
                        'pullback_depth': round(pullback_depth, 1),
                        'nearness_52w': round(nearness_52w, 1),
                        'quality_score': 100.0,
                        'year_high': round(year_high, 2)
                    }
                    return details, j
        return None, {}
    except:
        return None, {}

def check_entry_in_watchlist(df, watchlist_idx, entry_price):
    try:
        start_idx = watchlist_idx + 1
        if start_idx >= len(df): return False, {}
        end_search = min(start_idx + R['watchlist_days'], len(df))
        window = df.iloc[start_idx:end_search]
        for i in range(len(window)):
            if window['High'].iloc[i] > entry_price:
                entry_idx = start_idx + i
                entry_date = window.index[i]
                sl_price = entry_price * (1 - R['sl_pct']/100)
                target_price = entry_price * (1 + R['target_pct']/100)
                exit_idx = min(entry_idx + R['hold_days'], len(df) - 1)
                exit_price = float(df['Close'].iloc[exit_idx])
                exit_date = df.index[exit_idx]
                result = f'Exit_{R["hold_days"]}D'
                for k in range(entry_idx + 1, exit_idx + 1):
                    h, l = df['High'].iloc[k], df['Low'].iloc[k]
                    if l <= sl_price:
                        exit_price = sl_price
                        exit_date = df.index[k]
                        result = f'SL -{R["sl_pct"]}%'
                        break
                    if h >= target_price:
                        exit_price = target_price
                        exit_date = df.index[k]
                        result = f'Target +{R["target_pct"]}%'
                        break
                pl_pct = ((exit_price - entry_price) / entry_price) * 100
                hold_days = (exit_date - entry_date).days
                return True, {
                    'entry_date': entry_date.strftime('%Y-%m-%d'),
                    'exit_date': exit_date.strftime('%Y-%m-%d'),
                    'exit_price': round(exit_price, 2),
                    'hold_days': int(hold_days),
                    'pl_pct': round(pl_pct, 2),
                    'result': result
                }
        return False, {}
    except:
        return False, {}

def backtest_stock_perfect_only(df_daily, ticker, start_date, end_date):
    df_daily = add_indicators(df_daily)
    if not check_liquidity(df_daily): return []
    # Filter data for this year only
    df_year = df_daily[(df_daily.index >= start_date) & (df_daily.index <= end_date)]
    if len(df_year) < 50: return []

    trades = []
    total_len = len(df_daily)
    if total_len < 252: return []

    for i in range(R['checks_per_stock']):
        check_end_idx = total_len - 1 - (i * R['gap_between_checks'])
        if check_end_idx < R['scan_window']: break
        if df_daily.index[check_end_idx] < start_date: break
        if df_daily.index[check_end_idx] > end_date: continue

        details, watchlist_idx = find_perfect_pullback_silent(df_daily, check_end_idx)
        if details is None: continue
        entry_ok, trade_details = check_entry_in_watchlist(df_daily, watchlist_idx, details['entry_price'])
        if not entry_ok: continue
        trades.append({'Stock': ticker, **details, **trade_details})
    return trades

# MAIN LOOP - YEAR BY YEAR
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

years = [2024, 2025, 2026] # Last 3 saal
all_year_results = []

for year in years:
    print(f"\n=== SCANNING YEAR {year} ===", flush=True)
    start_date = datetime(year, 1, 1)
    end_date = datetime(year, 12, 31)
    if year == 2026: end_date = datetime(2026, 6, 7) # Aaj tak

    signals = []
    for i, stock in enumerate(stocks):
        try:
            if i % 50 == 0:
                print(f"Year {year} Progress: {i}/{len(stocks)} | Found: {len(signals)}", flush=True)
            scan_start = start_date - timedelta(days=800)
            df = yf.download(f"{stock}.NS", start=scan_start, end=end_date + timedelta(days=40),
                            progress=False, auto_adjust=True, timeout=30)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if len(df) < 252: continue
            trades = backtest_stock_perfect_only(df, stock, start_date, end_date)
            signals.extend(trades)
            time.sleep(0.2)
        except:
            continue

    if signals:
        df_out = pd.DataFrame(signals)
        total_trades = len(df_out)
        win_trades = (df_out['pl_pct'] > 0).sum()
        win_rate = round(win_trades / total_trades * 100, 1) if total_trades > 0 else 0
        total_pl = round(df_out['pl_pct'].sum(), 2)
        avg_pl = round(df_out['pl_pct'].mean(), 2)

        all_year_results.append({
            'Year': year,
            'Trades': total_trades,
            'Win_Rate': win_rate,
            'Total_PL': total_pl,
            'Avg_PL': avg_pl
        })
        print(f"YEAR {year}: {total_trades} Trades | {win_rate}% Win | {total_pl}% Total | {avg_pl}% Avg", flush=True)
    else:
        all_year_results.append({
            'Year': year,
            'Trades': 0,
            'Win_Rate': 0,
            'Total_PL': 0,
            'Avg_PL': 0
        })
        print(f"YEAR {year}: 0 Trades", flush=True)

# OUTPUT TO SHEET
try:
    ws_output = sh.worksheet("Perfect_YearWise")
except:
    ws_output = sh.add_worksheet(title="Perfect_YearWise", rows=100, cols=10)

ws_output.clear()
df_year = pd.DataFrame(all_year_results)
payload = [df_year.columns.values.tolist()] + df_year.values.tolist()
ws_output.update('A1', payload)

print("\n=== YEAR WISE SUMMARY ===", flush=True)
print(df_year, flush=True)
