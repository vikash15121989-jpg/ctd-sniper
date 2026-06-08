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

print("=== V15.15 NO GAP MULTI-ENTRY ===", flush=True)

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

start_date = ref_date - timedelta(days=365)
print(f"Scan Range: {start_date.date()} to {ref_date.date()}", flush=True)

# 2. RULES - NO GAP
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'min_vol_shares': 100000,
    'uptrend_days': 10,
    'vol_ma_days': 20,
    'scan_window': 60,
    'silent_lookback': 10,
    'watchlist_days': 10,
    'hold_days': 10,
    # ATR RULES
    'atr_period': 14,
    'sl_atr_mult': 1.0,
    'target_atr_mult': 2.0,
    # SCORE FILTER - 80-90 FULL RANGE
    'score_min': 80,
    'score_max': 90,
    # GAP RULE HATA DIYA - min_days_between_trades: 0
    # ACCUMULATION FILTERS
    'accum_days': 10,
    'max_price_drop_10d': -3.0,
    'min_vol_growth': 0.85,
    'min_green_red_ratio': 1.1,
}

fail_log = {'Liquidity': 0, 'Data': 0, 'No_Uptrend': 0, 'No_Pullback': 0, 'No_Silent': 0,
            'No_Entry': 0, 'Distribution': 0, 'Score_Filter': 0}

def add_indicators(df):
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['New_High_10D'] = df['High'] > df['High'].shift(1).rolling(10).max()

    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR'] = true_range.rolling(R['atr_period']).mean()

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

def find_all_pullback_silent(df, year_start, year_end):
    setups = []
    total_len = len(df)

    for i in range(252, total_len):
        if df.index[i] < year_start or df.index[i] > year_end:
            continue

        # UPTREND CHECK
        uptrend_start = i - R['uptrend_days']
        if uptrend_start < 0: continue
        uptrend_zone = df.iloc[uptrend_start:i]
        new_highs = uptrend_zone['New_High_10D'].sum()
        avg_vol = uptrend_zone['Volume'].mean()
        vol_20ma = df['Vol_20MA'].iloc[i]
        if pd.isna(vol_20ma): continue
        uptrend = new_highs >= 3 and avg_vol > vol_20ma
        if not uptrend:
            fail_log['No_Uptrend'] += 1
            continue

        # PULLBACK MARK
        row = df.iloc[i]
        vol_max_10d = row['Vol_10D_Max']
        high_max_10d = row['High_10D_Max']
        if pd.isna(vol_max_10d) or pd.isna(high_max_10d): continue
        pullback_cond1 = row['High'] < high_max_10d
        pullback_cond2 = row['Volume'] < vol_max_10d
        if not (pullback_cond1 and pullback_cond2):
            fail_log['No_Pullback'] += 1
            continue

        pullback_idx = i
        search_end = min(pullback_idx + 1 + R['watchlist_days'], len(df))

        for j in range(pullback_idx + 1, search_end):
            silent_row = df.iloc[j]
            vol_max_10d_silent = silent_row['Vol_10D_Max']
            high_max_10d_silent = silent_row['High_10D_Max']
            atr = silent_row['ATR']
            if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent) or pd.isna(atr): continue

            if silent_row['High'] > high_max_10d_silent and silent_row['Volume'] < vol_max_10d_silent:
                continue

            silent_cond1 = silent_row['Volume'] > vol_max_10d_silent
            silent_cond2 = silent_row['High'] < high_max_10d_silent

            if silent_cond1 and silent_cond2:
                # ACCUMULATION FILTER
                acc_start = max(0, j - R['accum_days'])
                acc_zone = df.iloc[acc_start:j]
                if len(acc_zone) < 5: continue

                price_change_10d = (silent_row['Close'] / acc_zone['Close'].iloc[0] - 1) * 100
                if price_change_10d < R['max_price_drop_10d']:
                    fail_log['Distribution'] += 1
                    continue

                first_half_vol = acc_zone['Volume'].iloc[:5].mean()
                second_half_vol = acc_zone['Volume'].iloc[5:].mean()
                if second_half_vol < first_half_vol * R['min_vol_growth']:
                    fail_log['Distribution'] += 1
                    continue

                green_vol = acc_zone[acc_zone['Close'] > acc_zone['Open']]['Volume'].sum()
                red_vol = acc_zone[acc_zone['Close'] < acc_zone['Open']]['Volume'].sum()
                green_red_ratio = green_vol / red_vol if red_vol > 0 else 99
                if green_red_ratio < R['min_green_red_ratio']:
                    fail_log['Distribution'] += 1
                    continue

                entry_price = high_max_10d_silent

                # QUALITY SCORE CALC
                pullback_depth = ((high_max_10d - row['Low']) / high_max_10d) * 100
                year_high = df['High'].iloc[max(0, j-252):j].max()
                nearness_52w = ((year_high - entry_price) / year_high) * 100
                vol_score = (silent_row['Volume'] / vol_max_10d_silent * 40)
                depth_score = (pullback_depth * 3)
                near_score = (max(0, 20-nearness_52w) * 1.5)
                score = vol_score + depth_score + near_score

                if score < R['score_min'] or score > R['score_max']:
                    fail_log['Score_Filter'] += 1
                    continue

                # ATR BASED SL/TP
                sl_price = entry_price - (atr * R['sl_atr_mult'])
                target_price = entry_price + (atr * R['target_atr_mult'])
                sl_pct = ((entry_price - sl_price) / entry_price) * 100
                target_pct = ((target_price - entry_price) / entry_price) * 100

                setups.append({
                    'watchlist_date': df.index[j],
                    'watchlist_idx': j,
                    'entry_price': round(entry_price, 2),
                    'atr': round(atr, 2),
                    'sl_price': round(sl_price, 2),
                    'target_price': round(target_price, 2),
                    'sl_pct': round(sl_pct, 2),
                    'target_pct': round(target_pct, 2),
                    'rr_ratio': round(R['target_atr_mult'] / R['sl_atr_mult'], 2),
                    'quality_score': round(score, 1),
                    'pullback_date': df.index[pullback_idx].strftime('%Y-%m-%d'),
                    'uptrend_start_date': df.index[uptrend_start].strftime('%Y-%m-%d'),
                })
                break

    return setups

def check_entry_in_watchlist(df, setup):
    try:
        watchlist_idx = setup['watchlist_idx']
        entry_price = setup['entry_price']
        sl_price = setup['sl_price']
        target_price = setup['target_price']

        start_idx = watchlist_idx + 1
        if start_idx >= len(df): return False, {}
        end_search = min(start_idx + R['watchlist_days'], len(df))
        window = df.iloc[start_idx:end_search]

        for i in range(len(window)):
            if window['High'].iloc[i] > entry_price:
                entry_idx = start_idx + i
                entry_date = window.index[i]
                exit_idx = min(entry_idx + R['hold_days'], len(df) - 1)
                exit_price = float(df['Close'].iloc[exit_idx])
                exit_date = df.index[exit_idx]
                result = f'Exit_{R["hold_days"]}D'

                for k in range(entry_idx + 1, exit_idx + 1):
                    h, l = df['High'].iloc[k], df['Low'].iloc[k]
                    if l <= sl_price:
                        exit_price = sl_price
                        exit_date = df.index[k]
                        result = 'SL_Hit'
                        break
                    if h >= target_price:
                        exit_price = target_price
                        exit_date = df.index[k]
                        result = 'Target_Hit'
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

def backtest_stock_no_gap(df_daily, ticker, year_start, year_end):
    df_daily = add_indicators(df_daily)
    if not check_liquidity(df_daily):
        fail_log['Liquidity'] += 1
        return []

    all_setups = find_all_pullback_silent(df_daily, year_start, year_end)
    if not all_setups:
        fail_log['No_Silent'] += 1
        return []

    trades = []
    # GAP CHECK HATA DIYA - HAR SETUP LE LO
    for setup in all_setups:
        entry_ok, trade_details = check_entry_in_watchlist(df_daily, setup)
        if not entry_ok:
            fail_log['No_Entry'] += 1
            continue
        trades.append({'Stock': ticker, **setup, **trade_details})

    return trades

# MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks - NO GAP + 80-90 SCORE", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)}", flush=True)

        download_start = start_date - timedelta(days=400)
        df = yf.download(f"{stock}.NS", start=download_start, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 252:
            fail_log['Data'] += 1
            continue

        trades = backtest_stock_no_gap(df, stock, start_date, ref_date)
        signals.extend(trades)
        time.sleep(0.2)
    except:
        continue

print(f"\nScan Complete. Total Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# OUTPUT
try:
    ws_output = sh.worksheet("NoGap_80_90")
except:
    ws_output = sh.add_worksheet(title="NoGap_80_90", rows=10000, cols=40)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out['watchlist_date'] = pd.to_datetime(df_out['watchlist_date'])
    df_out = df_out.sort_values(['Stock', 'watchlist_date'])

    def convert_to_native(val):
        if isinstance(val, (np.integer, np.int64)): return int(val)
        elif isinstance(val, (np.floating, np.float64)): return float(val)
        elif isinstance(val, pd.Timestamp): return val.strftime('%Y-%m-%d')
        else: return val
    df_out = df_out.applymap(convert_to_native)

    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    # STATS
    total_trades = len(df_out)
    win_trades = (df_out['pl_pct'] > 0).sum()
    win_rate = round(win_trades / total_trades * 100, 1)
    total_pl = round(df_out['pl_pct'].sum(), 2)
    avg_pl = round(df_out['pl_pct'].mean(), 1)
    avg_rr = round(df_out['rr_ratio'].mean(), 2)

    # Score bucket analysis
    bins = [80, 82, 84, 86, 88, 90]
    labels = ['80-82', '82-84', '84-86', '86-88', '88-90']
    df_out['Score_Bucket'] = pd.cut(df_out['quality_score'], bins=bins, labels=labels, include_lowest=True)
    score_analysis = df_out.groupby('Score_Bucket').agg({
        'Stock': 'count', 'pl_pct': ['sum', 'mean', lambda x: (x > 0).sum()]
    }).round(2)
    score_analysis.columns = ['Trades', 'Total_PL', 'Avg_PL', 'Wins']
    score_analysis['Win_Rate'] = (score_analysis['Wins'] / score_analysis['Trades'] * 100).round(1)
    score_analysis = score_analysis.drop('Wins', axis=1)

    # Stock wise count
    stock_counts = df_out['Stock'].value_counts().head(15)

    current_row = len(payload) + 3
    ws_output.update(f'A{current_row}', [[f'NO GAP 80-90 STATS: {start_date.date()} to {ref_date.date()}']])
    ws_output.update(f'A{current_row+1}', [
        ['Total Trades', total_trades], ['Win Rate %', win_rate],
        ['Total P&L %', total_pl], ['Avg P&L %', avg_pl],
        ['Avg RR', avg_rr], ['Unique Stocks', df_out['Stock'].nunique()],
        ['Avg Trades/Stock', round(total_trades/df_out['Stock'].nunique(), 1)]
    ])

    current_row += 9
    ws_output.update(f'A{current_row}', [['SCORE BUCKET ANALYSIS']])
    ws_output.update(f'A{current_row+1}', [score_analysis.reset_index().columns.values.tolist()] + score_analysis.reset_index().values.tolist())

    current_row += 8
    ws_output.update(f'A{current_row}', [['TOP 15 STOCKS BY TRADE COUNT']])
    ws_output.update(f'A{current_row+1}', [['Stock', 'Trades']] + [[k, int(v)] for k, v in stock_counts.items()])

    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN | {total_pl}% TOTAL | {df_out['Stock'].nunique()} STOCKS ===", flush=True)

else:
    ws_output.update('A1', [["No Signals Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
