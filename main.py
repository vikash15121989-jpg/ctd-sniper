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

print("=== V15.0 PULLBACK SILENT - TERA LOGIC ===", flush=True)

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

nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)
if ref_date.date() > nifty_df.index[-1].date():
    ref_date = nifty_df.index[-1].to_pydatetime()

print(f"Scan Till: {ref_date.date()}", flush=True)

# 2. RULES - TERA LOGIC
R = {
    # LIQUIDITY
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'min_vol_shares': 100000,

    # UPTREND CHECK
    'uptrend_days': 10, # Pichle 10 din me high ban raha ho
    'vol_ma_days': 20, # Volume 20MA se compare

    # SCAN WINDOW
    'scan_window': 60, # Pichle 60 din me setup dhoondo

    # SILENT & ENTRY
    'silent_lookback': 10, # 10 din ka max vol/high
    'watchlist_days': 10, # 10 din tak watchlist me
    'target_pct': 15,
    'sl_pct': 8,
    'hold_days': 30,

    # SAMPLING
    'checks_per_stock': 4,
    'gap_between_checks': 60,
}

fail_log = {'Liquidity': 0, 'Data': 0, 'No_Uptrend': 0, 'No_Pullback': 0, 'No_Silent': 0, 'No_Entry': 0}

def add_indicators(df):
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    # Naya high bana ya nahi
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

def find_pullback_silent(df, end_idx):
    """
    60 DIN SCAN KARO:
    1. Uptrend dhoondo - Naye high + Volume support
    2. Pullback mark karo - High nahi tuta + Vol kam
    3. Silent entry - Vol fata + High nahi tuta = Watchlist
    """
    try:
        start_idx = max(0, end_idx - R['scan_window'] + 1)

        for i in range(start_idx, end_idx + 1):
            if i < 20: continue

            # STEP 1: UPTREND CHECK - Kya pehle high ban raha tha?
            uptrend_start = i - R['uptrend_days']
            if uptrend_start < 0: continue

            uptrend_zone = df.iloc[uptrend_start:i]
            # Pichle 10 din me kam se kam 3 naye high
            new_highs = uptrend_zone['New_High_10D'].sum()
            # Volume 20MA se upar
            avg_vol = uptrend_zone['Volume'].mean()
            vol_20ma = df['Vol_20MA'].iloc[i]

            if pd.isna(vol_20ma): continue
            uptrend = new_highs >= 3 and avg_vol > vol_20ma

            if not uptrend: continue

            # STEP 2: PULLBACK MARK - High ruka + Vol kam
            row = df.iloc[i]
            vol_max_10d = row['Vol_10D_Max']
            high_max_10d = row['High_10D_Max']

            if pd.isna(vol_max_10d) or pd.isna(high_max_10d): continue

            # High break nahi kiya
            pullback_cond1 = row['High'] < high_max_10d
            # Volume bhi kam hai
            pullback_cond2 = row['Volume'] < vol_max_10d

            if not (pullback_cond1 and pullback_cond2): continue

            # Pullback mil gaya - Ab iske aage silent dhoondo
            pullback_idx = i

            # STEP 3: SILENT ENTRY - Pullback ke baad 10 din me
            search_end = min(pullback_idx + 1 + R['watchlist_days'], len(df))

            for j in range(pullback_idx + 1, search_end):
                silent_row = df.iloc[j]
                vol_max_10d_silent = silent_row['Vol_10D_Max']
                high_max_10d_silent = silent_row['High_10D_Max']

                if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent): continue

                # Bina volume ke high break = Ignore
                if silent_row['High'] > high_max_10d_silent and silent_row['Volume'] < vol_max_10d_silent:
                    continue

                # SILENT CONDITION: Vol fata + High nahi tuta
                silent_cond1 = silent_row['Volume'] > vol_max_10d_silent
                silent_cond2 = silent_row['High'] < high_max_10d_silent

                if silent_cond1 and silent_cond2:
                    # WATCHLIST ME DALO
                    watchlist_date = df.index[j]
                    entry_price = high_max_10d_silent # Pichle 10 din ka max = Entry

                    details = {
                        'uptrend_start_date': df.index[uptrend_start].strftime('%Y-%m-%d'),
                        'pullback_date': df.index[pullback_idx].strftime('%Y-%m-%d'),
                        'pullback_high': round(row['High'], 2),
                        'pullback_vol_ratio': round(row['Volume'] / vol_max_10d, 2),
                        'watchlist_date': watchlist_date.strftime('%Y-%m-%d'),
                        'watchlist_idx': j,
                        'silent_vol': int(silent_row['Volume']),
                        'silent_vol_10d_max': int(vol_max_10d_silent),
                        'silent_vol_ratio': round(silent_row['Volume'] / vol_max_10d_silent, 2),
                        'entry_price': round(entry_price, 2),
                        'watchlist_expiry': (watchlist_date + timedelta(days=R['watchlist_days'])).strftime('%Y-%m-%d')
                    }
                    return details, j

        return None, {}
    except:
        return None, {}

def check_entry_in_watchlist(df, watchlist_idx, entry_price):
    """
    STEP 4: Watchlist me aane ke 10 din me entry price break kare to Entry
    """
    try:
        start_idx = watchlist_idx + 1
        if start_idx >= len(df): return False, {}

        end_search = min(start_idx + R['watchlist_days'], len(df))
        window = df.iloc[start_idx:end_search]

        for i in range(len(window)):
            if window['High'].iloc[i] > entry_price:
                # ENTRY MIL GAYA
                entry_idx = start_idx + i
                entry_date = window.index[i]

                # EXIT
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

        return False, {'reason': 'No_Breakout_10D'}
    except:
        return False, {}

def backtest_stock_pullback_silent(df_daily, ticker):
    df_daily = add_indicators(df_daily)

    if not check_liquidity(df_daily):
        fail_log['Liquidity'] += 1
        return []

    trades = []
    total_len = len(df_daily)
    if total_len < 200:
        fail_log['Data'] += 1
        return []

    for i in range(R['checks_per_stock']):
        check_end_idx = total_len - 1 - (i * R['gap_between_checks'])
        if check_end_idx < R['scan_window']: continue

        # PULLBACK + SILENT DHUNDO
        details, watchlist_idx = find_pullback_silent(df_daily, check_end_idx)
        if details is None:
            fail_log['No_Silent'] += 1
            continue

        # ENTRY DHUNDO
        entry_ok, trade_details = check_entry_in_watchlist(df_daily, watchlist_idx, details['entry_price'])

        if not entry_ok:
            fail_log['No_Entry'] += 1
            continue

        trades.append({
            'Stock': ticker,
            **details,
            **trade_details
        })

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks - PULLBACK SILENT MODE...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)} | Fail: L:{fail_log['Liquidity']} S:{fail_log['No_Silent']} E:{fail_log['No_Entry']}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200:
            fail_log['Data'] += 1
            continue

        trades = backtest_stock_pullback_silent(df, stock)
        if len(trades) == 0: continue

        for trade in trades:
            print(f"🎯 {stock} Pullback:{trade['pullback_date']} | Watchlist:{trade['watchlist_date']} Vol:{trade['silent_vol_ratio']}x | Entry:{trade['entry_date']} @ {trade['entry_price']} | {trade['pl_pct']}%", flush=True)
            signals.append(trade)
        time.sleep(0.2)
    except Exception as e:
        continue

print(f"\nScan Complete. Total Pullback Silent Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Pullback_Silent")
except:
    ws_output = sh.add_worksheet(title="Pullback_Silent", rows=5000, cols=25)

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
    avg_pl = round(df_out['pl_pct'].mean(), 1)

    summary = [
        ['', ''], ['TOTAL PULLBACK SILENT SIGNALS', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG P&L PER TRADE %', float(avg_pl)],
        ['AVG SILENT VOL RATIO', float(df_out['silent_vol_ratio'].mean())],
        ['', ''], ['FAIL REASONS', ''],
        ['Liquidity Fail', int(fail_log['Liquidity'])],
        ['No Pullback+Silent', int(fail_log['No_Silent'])],
        ['Watchlist But No Entry', int(fail_log['No_Entry'])],
        ['Data Error', int(fail_log['Data'])],
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN | {total_pl}% TOTAL ===", flush=True)
else:
    ws_output.update('A1', [["No Pullback Silent Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
    print(f"Fail Log: {fail_log}", flush=True)
