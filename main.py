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

print("=== V13.2 DEMAND DOMINANCE - BUYER BEAT SELLER ===", flush=True)

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

# 2. RULES - DEMAND > SUPPLY
R = {
    # RULE 0: LIQUIDITY FILTER
    'min_price': 100,
    'min_daily_value_cr': 1, # 1 Cr daily
    'min_vol_shares': 200000, # 2L shares

    # RULE 1: DEMAND DOMINANCE
    'lookback_days': 60,
    'up_vol_vs_down': 1.5, # Up day vol > 1.5x Down day vol
    'close_position': 0.65, # Avg close > 65% of daily range
    'down_day_vol_ratio': 0.7, # Down din vol < 0.7x avg = Sukha dip
    'accumulation_days': 35, # 60 me se 35 din green minimum
    'delivery_proxy': 3.0, # Up din me 3%+ gain + 1.5x vol = Delivery

    # RULE 2: SUPPLY WEAK
    'max_drawdown': 10, # 10% se zyada dip nahi = Seller kamjor
    'max_consecutive_red': 3, # Lagatar 3 din laal nahi

    # FINAL SCORE
    'demand_score': 70, # 100 me se 70 min
}

fail_log = {
    'Liquidity': 0, 'UpVol': 0, 'ClosePos': 0, 'DownVol': 0,
    'Accumulation': 0, 'Drawdown': 0, 'Data': 0
}

def add_indicators(df):
    df['Returns'] = df['Close'].pct_change() * 100
    df['Up_Day'] = df['Returns'] > 0
    df['Range'] = df['High'] - df['Low']
    df['Close_Pos'] = (df['Close'] - df['Low']) / df['Range'] # 0=Low, 1=High
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Vol_Ratio'] = df['Volume'] / df['Vol_20MA']
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    return df

def check_liquidity(df, idx):
    """RULE 0: Kachra hatao"""
    try:
        close = df['Close'].iloc[idx]
        vol_20ma = df['Vol_20MA'].iloc[idx]
        daily_val = df['Daily_Value_20MA'].iloc[idx]

        if close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

def check_demand_dominance(df, idx):
    """
    RULE 1: DEMAND > SUPPLY PROOF
    Buyer roz seller ko peet raha hai ya nahi
    """
    try:
        window = df.iloc[idx-R['lookback_days']+1:idx+1]
        if len(window) < R['lookback_days']: return False, 0, {}

        up_days = window[window['Up_Day']]
        down_days = window[~window['Up_Day']]

        if len(up_days) < 10 or len(down_days) < 5:
            return False, 0, {}

        # 1. Up Day Volume vs Down Day Volume
        up_vol = up_days['Volume'].mean()
        down_vol = down_days['Volume'].mean()
        up_down_ratio = up_vol / down_vol if down_vol > 0 else 10

        # 2. Close Position - Kaha band ho raha
        avg_close_pos = window['Close_Pos'].mean()

        # 3. Down Day Volume - Dip pe sukha ya nahi
        down_vol_ratio = down_days['Vol_Ratio'].mean()

        # 4. Accumulation Days - Kitne din green
        accumulation = len(up_days)

        # 5. Delivery Proxy - Mal utha raha ya nahi
        delivery_days = up_days[
            (up_days['Returns'] > R['delivery_proxy']) &
            (up_days['Vol_Ratio'] > 1.5)
        ]
        delivery_count = len(delivery_days)

        # 6. Max Drawdown & Consecutive Red
        cumulative = (1 + window['Returns']/100).cumprod()
        running_max = cumulative.expanding().max()
        drawdown = ((cumulative - running_max) / running_max * 100).min()

        # Consecutive red days
        red_streak = 0
        max_red_streak = 0
        for ret in window['Returns']:
            if ret < 0:
                red_streak += 1
                max_red_streak = max(max_red_streak, red_streak)
            else:
                red_streak = 0

        # DEMAND SCORE - 100 ME SE
        score = 0
        score += min((up_down_ratio / R['up_vol_vs_down']) * 25, 25) # 25 marks
        score += min((avg_close_pos / R['close_position']) * 25, 25) # 25 marks
        score += min((R['down_day_vol_ratio'] / down_vol_ratio) * 15, 15) if down_vol_ratio > 0 else 15 # 15 marks
        score += min((accumulation / R['accumulation_days']) * 20, 20) # 20 marks
        score += min((delivery_count / 5) * 15, 15) # 15 marks - 5 din delivery = full

        # PASS CONDITIONS
        cond1 = up_down_ratio >= R['up_vol_vs_down']
        cond2 = avg_close_pos >= R['close_position']
        cond3 = down_vol_ratio <= R['down_day_vol_ratio']
        cond4 = accumulation >= R['accumulation_days']
        cond5 = drawdown >= -R['max_drawdown']
        cond6 = max_red_streak <= R['max_consecutive_red']
        cond7 = score >= R['demand_score']

        if not cond1: fail_log['UpVol'] += 1
        if not cond2: fail_log['ClosePos'] += 1
        if not cond3: fail_log['DownVol'] += 1
        if not cond4: fail_log['Accumulation'] += 1
        if not cond5: fail_log['Drawdown'] += 1

        all_cond = cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7

        details = {
            'up_down_vol': round(up_down_ratio, 2),
            'close_pos': round(avg_close_pos, 2),
            'down_vol_ratio': round(down_vol_ratio, 2),
            'accum_days': int(accumulation),
            'delivery_days': int(delivery_count),
            'max_dd': round(drawdown, 1),
            'red_streak': int(max_red_streak),
            'score': round(score, 1),
            'daily_val_cr': round(df['Daily_Value_20MA'].iloc[idx]/1e7, 1)
        }

        return all_cond, round(score, 1), details
    except:
        fail_log['Data'] += 1
        return False, 0, {}

def backtest_demand(df_daily, end_date, ticker):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 90:
        fail_log['Data'] += 1
        return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 90

    while i < len(df_daily) - 20:
        # STEP 1: LIQUIDITY
        if not check_liquidity(df_daily, i):
            fail_log['Liquidity'] += 1
            i += 5; continue

        # STEP 2: DEMAND DOMINANCE
        demand_ok, score, details = check_demand_dominance(df_daily, i)
        if not demand_ok:
            i += 5; continue

        # ENTRY - Demand > Supply proof mila
        entry_price = float(df_daily['Close'].iloc[i])
        entry_date = df_daily.index[i]

        # EXIT - 20 din ya 15% target ya 6% SL
        exit_idx = min(i + 20, len(df_daily) - 1)
        sl_price = entry_price * 0.94 # 6% SL
        target_price = entry_price * 1.15 # 15% Target

        result = 'Time Exit 20D'
        exit_price = float(df_daily['Close'].iloc[exit_idx])
        exit_date = df_daily.index[exit_idx]

        for k in range(i+1, exit_idx+1):
            h, l = df_daily['High'].iloc[k], df_daily['Low'].iloc[k]
            if l <= sl_price:
                exit_price, exit_date, result = sl_price, df_daily.index[k], 'SL -6%'; break
            if h >= target_price:
                exit_price, exit_date, result = target_price, df_daily.index[k], 'Target +15%'; break

        pl_pct = ((exit_price - entry_price) / entry_price) * 100
        days = (exit_date - entry_date).days

        trades.append({
            'entry_date': entry_date.strftime('%Y-%m-%d'),
            'demand_score': score,
            'daily_val_cr': details['daily_val_cr'],
            'up_down_vol': details['up_down_vol'],
            'close_pos': details['close_pos'],
            'accum_days': details['accum_days'],
            'delivery_days': details['delivery_days'],
            'max_dd': details['max_dd'],
            'entry_price': round(entry_price, 2),
            'exit_price': round(exit_price, 2),
            'days': int(days),
            'pl_pct': round(pl_pct, 2),
            'result': result
        })

        i = k + 5
        continue

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks for DEMAND > SUPPLY...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 100 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 90: continue

        trades = backtest_demand(df, ref_date, stock)
        if len(trades) == 0: continue

        for trade in trades:
            print(f"🔥 {stock} {trade['entry_date']} | Score:{trade['demand_score']} | Val:{trade['daily_val_cr']}Cr | UpVol:{trade['up_down_vol']}x | Close:{trade['close_pos']} | DelDays:{trade['delivery_days']} | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append({'Stock': stock, **trade})
        time.sleep(0.2)
    except Exception as e:
        continue

print(f"\nScan Complete. Total Demand Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Demand_Dominance")
except:
    ws_output = sh.add_worksheet(title="Demand_Dominance", rows=5000, cols=20)

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

    summary = [
        ['', ''], ['TOTAL DEMAND SIGNALS', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG DEMAND SCORE', float(df_out['demand_score'].mean())],
        ['AVG UP/DOWN VOL', float(df_out['up_down_vol'].mean())],
        ['AVG DAILY VALUE CR', float(df_out['daily_val_cr'].mean())],
        ['', ''], ['FAIL REASONS', ''],
        ['Liquidity Fail', int(fail_log['Liquidity'])],
        ['UpVol < 1.5x', int(fail_log['UpVol'])],
        ['ClosePos < 0.65', int(fail_log['ClosePos'])],
        ['DownVol > 0.7x', int(fail_log['DownVol'])],
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} DEMAND SIGNALS | {win_rate}% WIN ===", flush=True)
    print("\nTOP 10 DEMAND DOMINANCE:", flush=True)
    print(df_out[['Stock', 'entry_date', 'demand_score', 'up_down_vol', 'daily_val_cr', 'pl_pct']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Demand Dominance Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
