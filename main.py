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

print("=== V14.6 AGGRESSIVE POINT DETECTOR ===", flush=True)

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

    # STEP 1: AGGRESSIVE DETECTION - KITNA PICHLA DATA
    'agg_lookback': 20, # Pichle 20 din check karo
    'agg_up_days_min': 12, # 20 me 12 din green = 60%
    'agg_up_vol_ratio': 1.3, # Up vol > 1.3x Down vol
    'agg_close_pos': 0.60, # Avg close 60%+ range me
    'agg_max_dd': 15, # 15% se zyada dip nahi

    # STEP 2: SILENT ACTIVATION - AGGRESSIVE KE BAAD
    'activation_lookback': 10, # Pichle 10 din se compare
    'scan_window': 60, # Pichle 60 din me aggressive point dhoondo
    'entry_window': 10, # Aggressive ke 10 din me entry

    # EXIT
    'target_pct': 15,
    'sl_pct': 6,
    'hold_days': 30,

    # SAMPLING
    'checks_per_stock': 4,
    'gap_between_checks': 60,
}

fail_log = {'Liquidity': 0, 'Data': 0, 'No_Aggressive': 0, 'No_Entry': 0}

def add_indicators(df):
    df['Returns'] = df['Close'].pct_change() * 100
    df['Up_Day'] = df['Returns'] > 0
    df['Range'] = df['High'] - df['Low']
    df['Close_Pos'] = np.where(df['Range'] > 0, (df['Close'] - df['Low']) / df['Range'], 0.5)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
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

def find_aggressive_point(df, end_idx):
    """
    STEP 1: 60 DIN ME SCAN KARO
    HAR DIN PE PICHLE 20 DIN CHECK KARO - KYA BUYER AGGRESSIVE HUA?
    """
    try:
        start_idx = max(0, end_idx - R['scan_window'] + 1)

        # 60 din me har din check karo
        for i in range(start_idx, end_idx + 1):
            if i < R['agg_lookback']: continue # Shuru ke 20 din skip

            # Is din tak ke pichle 20 din ka data
            zone_start = i - R['agg_lookback'] + 1
            zone = df.iloc[zone_start:i+1]

            if len(zone) < 15: continue

            up_days = zone[zone['Up_Day']]
            down_days = zone[~zone['Up_Day']]

            if len(up_days) < 5 or len(down_days) < 3: continue

            # CONDITION 1: 20 me 12+ din green
            up_count = len(up_days)
            cond1 = up_count >= R['agg_up_days_min']

            # CONDITION 2: Up vol > 1.3x Down vol
            up_vol = up_days['Volume'].mean()
            down_vol = down_days['Volume'].mean()
            up_down_ratio = up_vol / down_vol if down_vol > 0 else 10
            cond2 = up_down_ratio >= R['agg_up_vol_ratio']

            # CONDITION 3: Avg close 60%+ range me
            avg_close_pos = zone['Close_Pos'].mean()
            cond3 = avg_close_pos >= R['agg_close_pos']

            # CONDITION 4: Max DD < 15%
            cumulative = (1 + zone['Returns']/100).cumprod()
            running_max = cumulative.expanding().max()
            drawdown = ((cumulative - running_max) / running_max * 100).min()
            cond4 = drawdown >= -R['agg_max_dd']

            if cond1 and cond2 and cond3 and cond4:
                # BUYER AGGRESSIVE POINT MIL GAYA
                aggressive_details = {
                    'agg_date': df.index[i].strftime('%Y-%m-%d'),
                    'agg_idx': i,
                    'agg_price': round(df['Close'].iloc[i], 2),
                    'agg_up_days': int(up_count),
                    'agg_up_down_vol': round(up_down_ratio, 2),
                    'agg_close_pos': round(avg_close_pos, 2),
                    'agg_max_dd': round(drawdown, 1),
                    'agg_zone_gain': round((df['Close'].iloc[i] / df['Close'].iloc[zone_start] - 1) * 100, 1)
                }
                return aggressive_details, i

        return None, {}
    except:
        return None, {}

def check_entry_after_aggressive(df, agg_idx):
    """
    STEP 2: AGGRESSIVE POINT KE BAAD 10 DIN ME
    Volume > 10D Max AND High < 10D Max WALA DIN DHUNDO
    USKE BAAD HIGH TOD DE TO ENTRY
    """
    try:
        start_idx = agg_idx + 1
        if start_idx >= len(df): return False, {}

        # Aggressive ke baad 10 din scan karo
        end_search = min(start_idx + R['entry_window'], len(df))

        for i in range(start_idx, end_search):
            if i < 10: continue

            row = df.iloc[i]

            # SILENT ACTIVATION CHECK
            vol_max_10d = row['Vol_10D_Max']
            high_max_10d = row['High_10D_Max']

            if pd.isna(vol_max_10d) or pd.isna(high_max_10d): continue

            cond1 = row['Volume'] > vol_max_10d
            cond2 = row['High'] < high_max_10d

            if cond1 and cond2:
                # SILENT POINT MILA - AB ENTRY CHECK
                resistance = high_max_10d

                # Is din ke baad high tuta kya?
                for j in range(i + 1, min(i + R['entry_window'], len(df))):
                    if df['High'].iloc[j] > resistance:
                        # ENTRY MIL GAYA
                        entry_idx = j
                        entry_date = df.index[j]
                        entry_price = resistance

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
                            'silent_date': df.index[i].strftime('%Y-%m-%d'),
                            'silent_vol_ratio': round(row['Volume'] / vol_max_10d, 2),
                            'resistance_10d': round(resistance, 2),
                            'entry_date': entry_date.strftime('%Y-%m-%d'),
                            'entry_price': round(entry_price, 2),
                            'exit_date': exit_date.strftime('%Y-%m-%d'),
                            'exit_price': round(exit_price, 2),
                            'hold_days': int(hold_days),
                            'pl_pct': round(pl_pct, 2),
                            'result': result
                        }

        return False, {'reason': 'No_Entry_10D'}

    except:
        return False, {}

def backtest_stock_aggressive(df_daily, ticker):
    """60 DIN ME AGGRESSIVE POINT DHUNDO, FIR ENTRY"""
    df_daily = add_indicators(df_daily)

    if not check_liquidity(df_daily):
        fail_log['Liquidity'] += 1
        return []

    trades = []
    total_len = len(df_daily)
    if total_len < 200:
        fail_log['Data'] += 1
        return []

    # 4 check points
    for i in range(R['checks_per_stock']):
        check_end_idx = total_len - 1 - (i * R['gap_between_checks'])
        if check_end_idx < R['scan_window']: continue

        # STEP 1: AGGRESSIVE POINT DHUNDO
        agg_details, agg_idx = find_aggressive_point(df_daily, check_end_idx)
        if agg_details is None:
            fail_log['No_Aggressive'] += 1
            continue

        # STEP 2: ENTRY DHUNDO
        entry_ok, trade_details = check_entry_after_aggressive(df_daily, agg_idx)

        if not entry_ok:
            fail_log['No_Entry'] += 1
            continue

        # TRADE BANA
        trades.append({
            'Stock': ticker,
            **agg_details,
            **trade_details
        })

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks - AGGRESSIVE POINT MODE...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)} | Fail: L:{fail_log['Liquidity']} Agg:{fail_log['No_Aggressive']} E:{fail_log['No_Entry']}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200:
            fail_log['Data'] += 1
            continue

        trades = backtest_stock_aggressive(df, stock)
        if len(trades) == 0: continue

        for trade in trades:
            print(f"🎯 {stock} Agg:{trade['agg_date']} UpDays:{trade['agg_up_days']} Vol:{trade['agg_up_down_vol']}x | Entry:{trade['entry_date']} | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append(trade)
        time.sleep(0.2)
    except Exception as e:
        continue

print(f"\nScan Complete. Total Aggressive Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Aggressive_Point")
except:
    ws_output = sh.add_worksheet(title="Aggressive_Point", rows=5000, cols=30)

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
        ['', ''], ['TOTAL AGGRESSIVE SIGNALS', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG P&L PER TRADE %', float(avg_pl)],
        ['AVG AGG UP DAYS', float(df_out['agg_up_days'].mean())],
        ['AVG UP/DOWN VOL', float(df_out['agg_up_down_vol'].mean())],
        ['', ''], ['FAIL REASONS', ''],
        ['Liquidity Fail', int(fail_log['Liquidity'])],
        ['No Aggressive Point in 60D', int(fail_log['No_Aggressive'])],
        ['Aggressive But No Entry', int(fail_log['No_Entry'])],
        ['Data Error', int(fail_log['Data'])],
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN | {total_pl}% TOTAL ===", flush=True)
    print("\nTOP 10 AGGRESSIVE TRADES:", flush=True)
    print(df_out[['Stock', 'agg_date', 'agg_up_days', 'agg_up_down_vol', 'entry_date', 'pl_pct', 'result']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Aggressive Points Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
    print(f"Fail Log: {fail_log}", flush=True)
