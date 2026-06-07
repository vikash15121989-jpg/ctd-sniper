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

print("=== V14.1 BUYER ACTIVATION POINT - 60D ME KAB JAAGA ===", flush=True)

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

# 2. RULES - ACTIVATION POINT
R = {
    # LIQUIDITY
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'min_vol_shares': 100000,

    # SCAN WINDOW
    'scan_window': 60, # Pichle 60 din me dekho
    'activation_lookback': 5, # Activation ke liye 5 din ka data

    # BUYER ACTIVATION SIGNAL - Jis din buyer jaaga
    'vol_spike': 2.0, # Vol 2x+ ho gaya
    'price_gain': 3.0, # Din ka gain 3%+
    'close_pos': 0.70, # High ke paas band
    'green_candle': True, # Green candle

    # BREAKOUT AFTER ACTIVATION
    'breakout_confirm_days': 10, # Activation ke baad 10 din me breakout
    'breakout_pct': 5, # Activation high + 5% = breakout
    'target_pct': 15, # 15% target
    'sl_pct': 6, # 6% SL from activation level
    'hold_days': 30, # Max 30 din hold

    # SAMPLING
    'checks_per_stock': 4, # Har stock me 4 bar
    'gap_between_checks': 60, # 60 din gap
}

fail_log = {'Liquidity': 0, 'Data': 0, 'No_Activation': 0, 'No_Breakout': 0}

def add_indicators(df):
    df['Returns'] = df['Close'].pct_change() * 100
    df['Up_Day'] = df['Returns'] > 0
    df['Range'] = df['High'] - df['Low']
    df['Close_Pos'] = np.where(df['Range'] > 0, (df['Close'] - df['Low']) / df['Range'], 0.5)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Vol_Ratio'] = df['Volume'] / df['Vol_20MA']
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
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

def find_buyer_activation(df, end_idx):
    """
    60 DIN ME SCAN - JIS DIN BUYER ACTIVE HUA WO POINT DHUNDO
    """
    try:
        start_idx = max(0, end_idx - R['scan_window'] + 1)
        window = df.iloc[start_idx:end_idx+1]
        if len(window) < 20: return None, {}

        # 60 din me har candle check karo
        for i in range(len(window)):
            idx = start_idx + i
            row = df.iloc[idx]

            # ACTIVATION SIGNAL - 4 condition
            cond1 = row['Up_Day'] == True # Green candle
            cond2 = row['Returns'] >= R['price_gain'] # 3%+ gain
            cond3 = row['Vol_Ratio'] >= R['vol_spike'] # 2x+ volume
            cond4 = row['Close_Pos'] >= R['close_pos'] # High close

            if cond1 and cond2 and cond3 and cond4:
                # BUYER ACTIVE POINT MIL GAYA
                activation_details = {
                    'activation_date': df.index[idx].strftime('%Y-%m-%d'),
                    'activation_idx': idx,
                    'activation_price': round(row['Close'], 2),
                    'activation_high': round(row['High'], 2),
                    'activation_gain': round(row['Returns'], 1),
                    'activation_vol': round(row['Vol_Ratio'], 1),
                    'activation_close_pos': round(row['Close_Pos'], 2),
                    'daily_val_cr': round(row['Daily_Value_20MA']/1e7, 1) if not pd.isna(row['Daily_Value_20MA']) else 0
                }
                return activation_details, idx

        return None, {}
    except:
        return None, {}

def check_breakout_from_activation(df, activation_idx, activation_high):
    """
    ACTIVATION POINT SE BREAKOUT HUA? KITNA RETURN?
    """
    try:
        start_idx = activation_idx + 1
        if start_idx >= len(df): return False, {}

        breakout_price = activation_high * (1 + R['breakout_pct']/100)
        breakout_idx = None
        breakout_date = None

        # Activation ke baad 10 din me breakout dekho
        end_search = min(start_idx + R['breakout_confirm_days'], len(df))
        window = df.iloc[start_idx:end_search]

        for i in range(len(window)):
            if window['High'].iloc[i] >= breakout_price:
                breakout_idx = start_idx + i
                breakout_date = window.index[i]
                break

        if breakout_idx is None:
            return False, {'reason': 'No_Breakout_10D'}

        # BREAKOUT KE BAAD RETURN
        entry_price = breakout_price
        sl_price = entry_price * (1 - R['sl_pct']/100)
        target_price = entry_price * (1 + R['target_pct']/100)

        exit_idx = min(breakout_idx + R['hold_days'], len(df) - 1)
        exit_price = float(df['Close'].iloc[exit_idx])
        exit_date = df.index[exit_idx]
        result = f'Exit_{R["hold_days"]}D'

        for k in range(breakout_idx + 1, exit_idx + 1):
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
        hold_days = (exit_date - breakout_date).days

        return True, {
            'breakout_date': breakout_date.strftime('%Y-%m-%d'),
            'breakout_price': round(entry_price, 2),
            'exit_date': exit_date.strftime('%Y-%m-%d'),
            'exit_price': round(exit_price, 2),
            'hold_days': int(hold_days),
            'pl_pct': round(pl_pct, 2),
            'result': result
        }

    except:
        return False, {}

def backtest_stock_activation(df_daily, ticker):
    """Har stock me 4 bar check - 60 din gap"""
    df_daily = add_indicators(df_daily)

    if not check_liquidity(df_daily):
        fail_log['Liquidity'] += 1
        return []

    trades = []
    total_len = len(df_daily)
    if total_len < 200:
        fail_log['Data'] += 1
        return []

    # 4 check points - peeche se
    for i in range(R['checks_per_stock']):
        check_end_idx = total_len - 1 - (i * R['gap_between_checks'])
        if check_end_idx < R['scan_window']: continue

        # STEP 1: 60 DIN ME ACTIVATION POINT DHUNDO
        activation, act_idx = find_buyer_activation(df_daily, check_end_idx)
        if activation is None:
            fail_log['No_Activation'] += 1
            continue

        # STEP 2: ACTIVATION SE BREAKOUT + RETURN
        breakout_ok, trade_details = check_breakout_from_activation(
            df_daily, act_idx, activation['activation_high']
        )

        if not breakout_ok:
            fail_log['No_Breakout'] += 1
            continue

        # TRADE BANA
        trades.append({
            'Stock': ticker,
            **activation,
            **trade_details
        })

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks - 60D ACTIVATION POINT MODE...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)} | Fail: L:{fail_log['Liquidity']} A:{fail_log['No_Activation']} B:{fail_log['No_Breakout']}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200:
            fail_log['Data'] += 1
            continue

        trades = backtest_stock_activation(df, stock)
        if len(trades) == 0: continue

        for trade in trades:
            print(f"🎯 {stock} Act:{trade['activation_date']} +{trade['activation_gain']}% Vol:{trade['activation_vol']}x | BO:{trade['breakout_date']} | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append(trade)
        time.sleep(0.2)
    except Exception as e:
        continue

print(f"\nScan Complete. Total Activation Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Buyer_Activation")
except:
    ws_output = sh.add_worksheet(title="Buyer_Activation", rows=5000, cols=25)

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
    avg_act_gain = round(df_out['activation_gain'].mean(), 1)
    avg_act_vol = round(df_out['activation_vol'].mean(), 1)

    summary = [
        ['', ''], ['TOTAL ACTIVATION TRADES', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG P&L PER TRADE %', float(avg_pl)],
        ['AVG ACTIVATION GAIN %', float(avg_act_gain)],
        ['AVG ACTIVATION VOL', float(avg_act_vol)],
        ['AVG HOLD DAYS', float(df_out['hold_days'].mean())],
        ['', ''], ['FAIL REASONS', ''],
        ['Liquidity Fail', int(fail_log['Liquidity'])],
        ['No Activation in 60D', int(fail_log['No_Activation'])],
        ['Activation But No Breakout', int(fail_log['No_Breakout'])],
        ['Data Error', int(fail_log['Data'])],
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN | {total_pl}% TOTAL ===", flush=True)
    print("\nTOP 10 ACTIVATION TRADES:", flush=True)
    print(df_out[['Stock', 'activation_date', 'activation_gain', 'activation_vol', 'breakout_date', 'pl_pct', 'result']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Buyer Activation Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
    print(f"Fail Log: {fail_log}", flush=True)
