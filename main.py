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

print("=== V13.4 HYBRID DEMAND - SEHWAG + DRAVID MODE ===", flush=True)

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

# 2. NIFTY CACHE
nifty_df = yf.download("^NSEI", period="10y", progress=False, auto_adjust=True)
if ref_date.date() > nifty_df.index[-1].date():
    ref_date = nifty_df.index[-1].to_pydatetime()

print(f"Scan Till: {ref_date.date()}", flush=True)

# 3. RULES - HYBRID MODE
R = {
    # RULE 0: LIQUIDITY - EK BAAR CHECK
    'min_price': 50, # 50 Rs minimum
    'min_daily_value_cr': 0.5, # 50 Lakh daily turnover minimum
    'min_vol_shares': 100000, # 1 Lakh shares minimum

    # RULE 1: HYBRID DEMAND
    'lookback_grinder': 60, # DRAVID MODE
    'lookback_spike': 5, # SEHWAG MODE

    # SEHWAG MODE - 5 Day Tabahi
    'spike_gain': 15, # 5 din me 15%+ gain
    'spike_vol': 2.0, # 5 din avg vol 2x+
    'spike_green': 4, # 5 din me 4 din green
    'spike_close': 0.70, # 5 din avg close 70%+ range

    # DRAVID MODE - 60 Day Grinder - 7 condition me 5 pass
    'up_vol_vs_down': 1.3, # Up day vol > 1.3x down day
    'close_position': 0.60, # Avg close > 60% range
    'down_day_vol_ratio': 0.8, # Down din vol < 0.8x avg
    'accumulation_days': 32, # 60 me 32 din green
    'max_drawdown': 15, # 15% se zyada dip nahi
    'max_consecutive_red': 4, # Lagatar 4 din laal nahi
    'gain_60d': 20, # 60 din me 20%+ gain
    'min_score': 5, # 7 me se 5 pass
}

fail_log = {
    'Liquidity': 0, 'Data': 0, 'Sehwag_Fail': 0,
    'Dravid_Fail': 0, 'Both_Fail': 0
}

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

def check_liquidity_ONCE(df):
    """RULE 0: Sirf latest 20 din ka avg check karo - Ek baar"""
    try:
        close = df['Close'].iloc[-1]
        vol_20ma = df['Vol_20MA'].iloc[-1]
        daily_val = df['Daily_Value_20MA'].iloc[-1]

        if pd.isna(close) or close < R['min_price']: return False, "Price"
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False, "Value"
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False, "Volume"
        return True, "Liquid"
    except:
        return False, "Data"

def check_demand_hybrid(df, idx):
    """
    HYBRID: MODE B pehle, phir MODE A
    MODE B: SEHWAG - 5 Day Spike
    MODE A: DRAVID - 60 Day Grinder
    """
    try:
        details = {}

        # ===== MODE B: SEHWAG SPIKE CHECK =====
        if idx >= R['lookback_spike']:
            window_5d = df.iloc[idx-R['lookback_spike']+1:idx+1]
            if len(window_5d) == R['lookback_spike']:
                spike_gain = (window_5d['Close'].iloc[-1] / window_5d['Close'].iloc[0] - 1) * 100
                spike_vol = window_5d['Vol_Ratio'].mean()
                spike_close = window_5d['Close_Pos'].mean()
                spike_green = window_5d['Up_Day'].sum()

                sehwag_pass = (
                    spike_gain >= R['spike_gain'] and
                    spike_vol >= R['spike_vol'] and
                    spike_green >= R['spike_green'] and
                    spike_close >= R['spike_close']
                )

                if sehwag_pass:
                    details = {
                        'mode': 'SEHWAG_SPIKE',
                        'gain_5d': round(spike_gain, 1),
                        'vol_5d': round(spike_vol, 1),
                        'green_5d': int(spike_green),
                        'close_5d': round(spike_close, 2),
                        'score': 10, # 10/7 = Full marks
                        'daily_val_cr': round(df['Daily_Value_20MA'].iloc[idx]/1e7, 1)
                    }
                    return True, 10, details
                else:
                    fail_log['Sehwag_Fail'] += 1

        # ===== MODE A: DRAVID GRINDER CHECK =====
        if idx < R['lookback_grinder']:
            return False, 0, {}

        window_60d = df.iloc[idx-R['lookback_grinder']+1:idx+1]
        if len(window_60d) < 50: return False, 0, {}

        up_days = window_60d[window_60d['Up_Day']]
        down_days = window_60d[~window_60d['Up_Day']]

        if len(up_days) < 10 or len(down_days) < 5:
            return False, 0, {}

        score = 0
        details = {'mode': 'DRAVID_GRINDER'}

        # 1. Up Day Volume vs Down
        up_vol = up_days['Volume'].mean()
        down_vol = down_days['Volume'].mean()
        up_down_ratio = up_vol / down_vol if down_vol > 0 else 10
        if up_down_ratio >= R['up_vol_vs_down']: score += 1
        details['up_down_vol'] = round(up_down_ratio, 2)

        # 2. Close Position
        avg_close_pos = window_60d['Close_Pos'].mean()
        if avg_close_pos >= R['close_position']: score += 1
        details['close_pos'] = round(avg_close_pos, 2)

        # 3. Down Day Volume Low
        down_vol_ratio = down_days['Vol_Ratio'].mean()
        if down_vol_ratio <= R['down_day_vol_ratio']: score += 1
        details['down_vol_ratio'] = round(down_vol_ratio, 2)

        # 4. Accumulation Days
        accumulation = len(up_days)
        if accumulation >= R['accumulation_days']: score += 1
        details['accum_days'] = int(accumulation)

        # 5. Max Drawdown
        cumulative = (1 + window_60d['Returns']/100).cumprod()
        running_max = cumulative.expanding().max()
        drawdown = ((cumulative - running_max) / running_max * 100).min()
        if drawdown >= -R['max_drawdown']: score += 1
        details['max_dd'] = round(drawdown, 1)

        # 6. Consecutive Red
        red_streak = 0
        max_red_streak = 0
        for ret in window_60d['Returns']:
            if ret < 0:
                red_streak += 1
                max_red_streak = max(max_red_streak, red_streak)
            else:
                red_streak = 0
        if max_red_streak <= R['max_consecutive_red']: score += 1
        details['red_streak'] = int(max_red_streak)

        # 7. 60 Day Gain
        gain_60d = (window_60d['Close'].iloc[-1] / window_60d['Close'].iloc[0] - 1) * 100
        if gain_60d >= R['gain_60d']: score += 1
        details['gain_60d'] = round(gain_60d, 1)

        details['score'] = score
        details['daily_val_cr'] = round(df['Daily_Value_20MA'].iloc[idx]/1e7, 1)

        if score >= R['min_score']:
            return True, score, details
        else:
            fail_log['Dravid_Fail'] += 1
            fail_log['Both_Fail'] += 1
            return False, score, details

    except Exception as e:
        fail_log['Data'] += 1
        return False, 0, {}

def backtest_hybrid(df_daily, end_date, ticker):
    df_daily = df_daily[df_daily.index <= end_date].copy()
    if len(df_daily) < 90:
        fail_log['Data'] += 1
        return []

    df_daily = add_indicators(df_daily)

    # STEP 1: LIQUIDITY EK BAAR CHECK
    liquid_ok, liquid_reason = check_liquidity_ONCE(df_daily)
    if not liquid_ok:
        fail_log['Liquidity'] += 1
        return []

    trades = []
    i = 90

    while i < len(df_daily) - 15: # 15 din hold minimum
        # STEP 2: HYBRID DEMAND CHECK
        demand_ok, score, details = check_demand_hybrid(df_daily, i)
        if not demand_ok:
            i += 3; continue # 3 din skip karo

        # ENTRY
        entry_price = float(df_daily['Close'].iloc[i])
        entry_date = df_daily.index[i]
        mode = details['mode']

        # EXIT LOGIC - Mode ke hisaab se
        if mode == 'SEHWAG_SPIKE':
            hold_days = 10 # Spike me 10 din hold
            target_pct = 1.12 # 12% target
            sl_pct = 0.94 # 6% SL
        else: # DRAVID_GRINDER
            hold_days = 20 # Grinder me 20 din
            target_pct = 1.15 # 15% target
            sl_pct = 0.94 # 6% SL

        exit_idx = min(i + hold_days, len(df_daily) - 1)
        sl_price = entry_price * sl_pct
        target_price = entry_price * target_pct

        result = f'Time Exit {hold_days}D'
        exit_price = float(df_daily['Close'].iloc[exit_idx])
        exit_date = df_daily.index[exit_idx]

        for k in range(i+1, exit_idx+1):
            h, l = df_daily['High'].iloc[k], df_daily['Low'].iloc[k]
            if l <= sl_price:
                exit_price, exit_date, result = sl_price, df_daily.index[k], 'SL -6%'; break
            if h >= target_price:
                exit_price, exit_date, result = target_price, df_daily.index[k], f'Target +{int((target_pct-1)*100)}%'; break

        pl_pct = ((exit_price - entry_price) / entry_price) * 100
        days = (exit_date - entry_date).days

        trades.append({
            'entry_date': entry_date.strftime('%Y-%m-%d'),
            'mode': mode,
            'score': score,
            'daily_val_cr': details['daily_val_cr'],
            **{k: v for k, v in details.items() if k not in ['mode', 'score', 'daily_val_cr']},
            'entry_price': round(entry_price, 2),
            'exit_price': round(exit_price, 2),
            'days': int(days),
            'pl_pct': round(pl_pct, 2),
            'result': result
        })

        i = k + 5 # Signal ke baad 5 din gap
        continue

    return trades

# 6. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks for HYBRID DEMAND...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 100 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)} | Fail: L:{fail_log['Liquidity']} S:{fail_log['Sehwag_Fail']} D:{fail_log['Dravid_Fail']}", flush=True)

        start_date = ref_date - timedelta(days=730)
        df = yf.download(f"{stock}.NS", start=start_date, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 90: continue

        trades = backtest_hybrid(df, ref_date, stock)
        if len(trades) == 0: continue

        for trade in trades:
            mode_icon = "⚡" if trade['mode'] == 'SEHWAG_SPIKE' else "📈"
            print(f"{mode_icon} {stock} {trade['entry_date']} | {trade['mode']} | Score:{trade['score']} | Val:{trade['daily_val_cr']}Cr | {trade['result']} {trade['pl_pct']}%", flush=True)
            signals.append({'Stock': stock, **trade})
        time.sleep(0.2)
    except Exception as e:
        continue

print(f"\nScan Complete. Total Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# 7. OUTPUT
try:
    ws_output = sh.worksheet("Demand_Hybrid")
except:
    ws_output = sh.add_worksheet(title="Demand_Hybrid", rows=5000, cols=25)

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

    sehwag_count = (df_out['mode'] == 'SEHWAG_SPIKE').sum()
    dravid_count = (df_out['mode'] == 'DRAVID_GRINDER').sum()

    summary = [
        ['', ''], ['TOTAL SIGNALS', int(total_trades)],
        ['WIN RATE %', float(win_rate)], ['TOTAL P&L %', float(total_pl)],
        ['SEHWAG SPIKES', int(sehwag_count)], ['DRAVID GRINDERS', int(dravid_count)],
        ['AVG DAILY VALUE CR', float(df_out['daily_val_cr'].mean())],
        ['', ''], ['FAIL REASONS', ''],
        ['Liquidity Fail', int(fail_log['Liquidity'])],
        ['Sehwag Mode Fail', int(fail_log['Sehwag_Fail'])],
        ['Dravid Mode Fail', int(fail_log['Dravid_Fail'])],
        ['Data Error', int(fail_log['Data'])],
    ]

    ws_output.update(f'A{len(payload)+2}', summary)
    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN ===", flush=True)
    print(f"SEHWAG: {sehwag_count} | DRAVID: {dravid_count}", flush=True)
    print("\nTOP 10:", flush=True)
    print(df_out[['Stock', 'entry_date', 'mode', 'score', 'daily_val_cr', 'pl_pct']].head(10), flush=True)
else:
    ws_output.update('A1', [["No Signals Found - Market Dry Hai"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
    print(f"Fail Log: {fail_log}", flush=True)
