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

print("=== V11.0 BUYER AGGRESSION - SELLER KAMJOR ===", flush=True)

# 1. SETUP - Same as before
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

print(f"A1 Date: {ref_date.date()}", flush=True)

# 2. NIFTY CACHE
print("Downloading Nifty...", flush=True)
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)
print(f"Nifty Done. Last Trading Date: {nifty_df.index[-1].date()}", flush=True)

if ref_date.date() > nifty_df.index[-1].date():
    ref_date = nifty_df.index[-1].to_pydatetime()

print(f"Final Scan Till: {ref_date.date()}", flush=True)

# 3. NEW RULES - BUYER AGGRESSION ONLY
R = {
    'up_vol_vs_down': 2.0, # Up day avg vol > 2x Down day avg vol
    'close_in_range': 0.70, # Close > 70% of daily range = High close
    'accumulation_days': 7, # 10 din me 7 din upar close
    'ema50_min': 1.05, # EMA50 se 5% upar minimum
    'ema50_max': 1.25, # EMA50 se 25% upar maximum - Fresh
    'breakout_vol': 2.0, # Breakout din 2x volume
    'higher_low': 3, # Har dip 3% upar
}

fail_log = {'Vol_Fail': 0, 'Close_Fail': 0, 'EMA_Fail': 0, 'Structure_Fail': 0, 'Data_Fail': 0}

def add_indicators(df):
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Range'] = df['High'] - df['Low']
    df['Close_Position'] = (df['Close'] - df['Low']) / df['Daily_Range'] # 0-1, 1=High pe close
    df['Up_Day'] = df['Close'] > df['Close'].shift(1)
    return df

def check_buyer_aggression(df, idx):
    """Rule 1: Up days pe volume, Down days se 2x"""
    try:
        window = df.iloc[idx-9:idx+1] # 10 din
        if len(window) < 10: return False, 0

        up_vol = window[window['Up_Day']]['Volume'].mean()
        down_vol = window[~window['Up_Day']]['Volume'].mean()

        if pd.isna(down_vol) or down_vol == 0:
            ratio = 10 # Down din hue hi nahi = Full buyer
        else:
            ratio = up_vol / down_vol

        return ratio >= R['up_vol_vs_down'], round(ratio, 2)
    except:
        return False, 0

def check_close_strength(df, idx):
    """Rule 2: Close hamesha range ke top me + 7/10 din upar"""
    try:
        window = df.iloc[idx-9:idx+1]
        if len(window) < 10: return False, 0, 0

        avg_close_pos = window['Close_Position'].mean() # 0.7+ chahiye
        up_days = window['Up_Day'].sum() # 7+ chahiye

        cond1 = avg_close_pos >= R['close_in_range']
        cond2 = up_days >= R['accumulation_days']

        return cond1 and cond2, round(avg_close_pos, 2), up_days
    except:
        return False, 0, 0

def check_fresh_momentum(df, idx):
    """Rule 3: EMA50 se 5-25% upar = Fresh, Extended nahi"""
    try:
        close = df['Close'].iloc[idx]
        ema50 = df['EMA50'].iloc[idx]
        if pd.isna(ema50) or ema50 == 0: return False, 0

        dist = close / ema50
        cond = R['ema50_min'] <= dist <= R['ema50_max']
        return cond, round((dist-1)*100, 1)
    except:
        return False, 0

def check_higher_low_structure(df, idx):
    """Rule 4: Dip kam vol pe, aur har dip pichle se upar"""
    try:
        window = df.iloc[idx-19:idx+1] # 20 din
        if len(window) < 20: return True, 0 # Data kam = Pass

        # 3 dips nikalo
        lows = window['Low'].rolling(5).min().dropna()
        if len(lows) < 3: return True, 0

        last_3_lows = lows.tail(3).values
        # Har low pichle se upar?
        hl_ok = last_3_lows[2] > last_3_lows[1] * (1 + R['higher_low']/100) > last_3_lows[0] * (1 + R['higher_low']/100)

        return hl_ok, round((last_3_lows[2]/last_3_lows[0]-1)*100, 1)
    except:
        return True, 0

def check_breakout_volume(df, idx):
    """Rule 5: Aaj ka volume 20MA se 2x"""
    try:
        vol = df['Volume'].iloc[idx]
        vol_20ma = df['Vol_20MA'].iloc[idx]
        if pd.isna(vol_20ma) or vol_20ma == 0: return True, 0

        ratio = vol / vol_20ma
        return ratio >= R['breakout_vol'], round(ratio, 2)
    except:
        return False, 0

def backtest_aggression(df_daily, end_date, ticker):
    global fail_log
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 60:
        fail_log['Data_Fail'] += 1
        return []

    df_daily = add_indicators(df_daily)
    trades = []
    i = 60 # 60 din data chahiye

    while i < len(df_daily) - 5:
        today = df_daily.iloc[i]

        # 5 CHECK - SAB PASS HONE CHAHIYE
        vol_ok, up_down_ratio = check_buyer_aggression(df_daily, i)
        if not vol_ok:
            fail_log['Vol_Fail'] += 1
            i += 3; continue

        close_ok, close_pos, up_days = check_close_strength(df_daily, i)
        if not close_ok:
            fail_log['Close_Fail'] += 1
            i += 3; continue

        ema_ok, ema_dist = check_fresh_momentum(df_daily, i)
        if not ema_ok:
            fail_log['EMA_Fail'] += 1
            i += 3; continue

        struct_ok, hl_pct = check_higher_low_structure(df_daily, i)
        if not struct_ok:
            fail_log['Structure_Fail'] += 1
            i += 3; continue

        breakout_ok, vol_x = check_breakout_volume(df_daily, i)
        if not breakout_ok:
            fail_log['Vol_Fail'] += 1
            i += 3; continue

        # ENTRY
        entry_price = float(today['Close'])
        sl = float(df_daily['Low'].iloc[i-10:i].min()) # 10 din ka low
        risk = entry_price - sl
        if risk <= 0: i += 3; continue

        target = entry_price + (risk * 2.5) # 2.5R kyunki strong buyer

        # EXIT SIMULATION
        exit_price, exit_date, days, result = entry_price, today.name, 0, 'Running'
        for k in range(i + 1, min(i + 60, len(df_daily))):
            days += 1
            h, l, c = df_daily['High'].iloc[k], df_daily['Low'].iloc[k], df_daily['Close'].iloc[k]
            if l <= sl:
                exit_price, exit_date, result = sl, df_daily.index[k], 'SL Hit'; break
            if h >= target:
                exit_price, exit_date, result = target, df_daily.index[k], 'Target Hit'; break
            if k == len(df_daily) - 1:
                exit_price, exit_date, result = float(c), df_daily.index[k], 'Running'

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        trades.append({
            'entry_date': today.name.strftime('%Y-%m-%d'),
            'up_down_vol': up_down_ratio,
            'close_pos': close_pos,
            'up_days': up_days,
            'ema50_dist': ema_dist,
            'hl_structure': hl_pct,
            'breakout_vol': vol_x,
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'exit_price': round(exit_price, 2),
            'days': int(days),
            'pl_pct': round(pl_pct, 2),
            'result': result
        })

        i = k + 3
        continue

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks for BUYER AGGRESSION...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 60:
            fail_log['Data_Fail'] += 1
            continue

        trades = backtest_aggression(df, ref_date, stock)
        if len(trades) == 0:
            continue

        for trade in trades:
            print(f"🔥 {stock} {trade['entry_date']} | UpVol:{trade['up_down_vol']}x | Close:{trade['close_pos']} | EMA50:{trade['ema50_dist']}% | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append({'Stock': stock, **trade})
        time.sleep(0.2)
    except Exception as e:
        if i % 100 == 0:
            print(f"Error {stock}: {str(e)[:60]}", flush=True)
        continue

print(f"Scan Complete. Total Aggression Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Buyer_Aggression")
except:
    ws_output = sh.add_worksheet(title="Buyer_Aggression", rows=5000, cols=20)

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
        ['', ''], ['TOTAL SIGNALS', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['AVG Up/Down Vol', float(df_out['up_down_vol'].mean())],
        ['AVG Close Position', float(df_out['close_pos'].mean())]
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN ===", flush=True)
    print("\nTOP 10 AGGRESSION:", flush=True)
    print(df_out[['Stock', 'entry_date', 'up_down_vol', 'ema50_dist', 'pl_pct']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Buyer Aggression Found - Market Weak"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
    print(f"Fail Reasons: {fail_log}", flush=True)
