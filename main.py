import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

BACKTEST_MODE = True
BACKTEST_END = datetime.now().date()
BACKTEST_START = BACKTEST_END - timedelta(days=365)
BATCH_SIZE = 50

print("=== RS BEATER V9.4 - VOLUME CONTRACTION + EXPANSION ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_daily_value_cr': 2.0, # 2Cr liquidity
    'fixed_target_pct': 6.0, # 6% TARGET LOCK
    'fixed_sl_pct': 2.5, # 2.5% SL = RR 1:2.4
    'rsi_min': 60,
    'rsi_max': 75,
    'breakout_buffer_pct': 0.3, # 10D high ke 0.3% upar
    'lookback_days': 10, # 10 din me high dhundo
    'min_volume_contraction_pct': 30.0, # High ke baad volume 30%+ sukha ho
    'volume_expansion_ratio': 1.2, # Breakout day volume > 1.2x of high day volume
    'time_stop_days': 10,
    'risk_per_trade': 1000,
    'cooldown_days': 15
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

print("\n Downloading Nifty 50 reference data...", flush=True)
nifty_df = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex): nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_df.index = nifty_df.index.strftime('%Y-%m-%d')
nifty_df = nifty_df[~nifty_df.index.duplicated(keep='last')]

def calculate_rs_1m_10d(df, i, current_date, nifty_df):
    try:
        if current_date not in nifty_df.index: return None, None
        nifty_idx = nifty_df.index.get_loc(current_date)

        periods = {'1M': 21, '10D': 10}
        rs_values = {}

        for name, days in periods.items():
            if i < days or nifty_idx < days: return None, None

            stock_start = df['Close'].iloc[i-days]
            stock_now = df['Close'].iloc[i]
            stock_ret = ((stock_now - stock_start) / stock_start) * 100

            nifty_start = nifty_df['Close'].iloc[nifty_idx-days]
            nifty_now = nifty_df['Close'].iloc[nifty_idx]
            nifty_ret = ((nifty_now - nifty_start) / nifty_start) * 100

            rs_values[name] = round(stock_ret - nifty_ret, 2)

        return rs_values['1M'], rs_values['10D']
    except:
        return None, None

def check_volume_pattern(df, i, debug_counter):
    """USER DEMAND: 10D High ka volume dekho, phir sukha ho, phir breakout pe explode"""
    if i < R['lookback_days']: return False, 0, 0

    # 1. Pichle 10 din ka Highest High aur uska index nikalo
    lookback_df = df.iloc[i-R['lookback_days']:i]
    if lookback_df.empty: return False, 0, 0

    max_high_idx = lookback_df['High'].idxmax()
    max_high = lookback_df.loc[max_high_idx, 'High']
    max_high_vol = lookback_df.loc[max_high_idx, 'Volume']
    max_high_pos = lookback_df.index.get_loc(max_high_idx)

    # 2. High ke baad volume sukha ho - Contraction
    after_high_df = lookback_df.iloc[max_high_pos+1:]
    if after_high_df.empty: return False, 0, 0 # High last day ka tha to skip

    avg_vol_after = after_high_df['Volume'].mean()
    if max_high_vol == 0: return False, 0, 0

    contraction_pct = ((max_high_vol - avg_vol_after) / max_high_vol) * 100
    if contraction_pct < R['min_volume_contraction_pct']:
        debug_counter['no_volume_contraction'] += 1
        return False, 0, 0

    # 3. Aaj ka candle - Breakout + Volume Expansion
    current_row = df.iloc[i]
    breakout_level = max_high * (1 + R['breakout_buffer_pct']/100)

    if current_row['Close'] <= breakout_level:
        debug_counter['no_breakout'] += 1
        return False, 0, 0

    # Breakout day ka volume > High day ka volume * 1.2
    if current_row['Volume'] < max_high_vol * R['volume_expansion_ratio']:
        debug_counter['no_volume_expansion'] += 1
        return False, 0, 0

    return True, max_high, max_high_vol

def check_entry(df, i, current_date, debug_counter):
    row = df.iloc[i]
    if pd.isna(row['EMA200']) or pd.isna(row['RSI']) or pd.isna(row['Vol_MA20']):
        debug_counter['nan'] += 1
        return False, 0, 0, 0

    # 1. Trend Filter
    trend = row['Close'] > row['EMA20'] > row['EMA50'] > row['EMA200']
    if not trend:
        debug_counter['trend'] += 1
        return False, 0, 0, 0

    # 2. RSI 60-75
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    if not rsi_ok:
        debug_counter['rsi'] += 1
        return False, 0, 0, 0

    # 3. USER DEMAND: RS Check pehle - Positive hona chahiye
    rs_1m, rs_10d = calculate_rs_1m_10d(df, i, current_date, nifty_df)
    if rs_1m is None or rs_10d is None:
        debug_counter['rs_error'] += 1
        return False, 0, 0, 0

    if rs_1m <= 0 or rs_10d <= 0: # Dono positive = Nifty beat
        debug_counter['rs_negative'] += 1
        return False, 0, 0, 0

    # 4. USER DEMAND: Volume Pattern - Contraction then Expansion
    vol_pattern_ok, high_10d, vol_at_high = check_volume_pattern(df, i, debug_counter)
    if not vol_pattern_ok:
        return False, 0, 0, 0

    return True, rs_1m, rs_10d, high_10d

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=400),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 200: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_indicators(df)
        df.index = df.index.strftime('%Y-%m-%d')
        df = df[~df.index.duplicated(keep='last')]
        return df, stock
    except:
        return None, stock

all_trades = []
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

print(f"\nTotal Watchlist: {total_stocks} stocks | Batches: {total_batches}", flush=True)
date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B').strftime('%Y-%m-%d')

debug_counter = {'nan':0, 'trend':0, 'rsi':0, 'rs_negative':0, 'no_volume_contraction':0, 'no_breakout':0, 'no_volume_expansion':0, 'liquidity':0, 'rs_error':0, 'cooldown':0}
total_candles_checked = 0
last_exit_dates = {}

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]

    print(f"\n{'='*60}", flush=True)
    print(f"BATCH {batch_num + 1}/{total_batches} | Stocks {start_idx+1}-{end_idx}", flush=True)

    stock_data = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_stock = {executor.submit(download_single_stock, stock): stock for stock in batch_stocks}
        for future in as_completed(future_to_stock):
            df, stock = future.result()
            if df is not None:
                stock_data[stock] = df

    open_positions = []

    for current_date in date_range:
        current_dt = pd.to_datetime(current_date).date()

        for pos in open_positions[:]:
            df = stock_data[pos['Stock']]
            if current_date not in df.index: continue
            row = df.loc[current_date]

            sl_hit = row['Low'] <= pos['SL']
            target_hit = row['High'] >= pos['Target']
            exit_price = None
            exit_status = None
            days_held = (current_dt - pd.to_datetime(pos['Entry_Date']).date()).days

            if sl_hit and target_hit:
                exit_price = pos['SL']; exit_status = 'LOSS'
            elif sl_hit:
                exit_price = pos['SL']; exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']; exit_status = 'WIN'
            elif days_held >= R['time_stop_days']:
                exit_price = row['Close']; exit_status = 'TIME'

            if exit_price:
                pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
                pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
                all_trades.append({
                    'Stock': pos['Stock'],
                    'Entry_Date': pos['Entry_Date'], 'Exit_Date': current_date,
                    'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
                    'Status': exit_status, 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
                    'Days_Held': days_held, 'RS_1M': pos['RS_1M'], 'RS_10D': pos['RS_10D'],
                    '10D_High': pos['10D_High'], 'Qty': pos['Qty']
                })
                last_exit_dates[pos['Stock']] = current_dt
                open_positions.remove(pos)

        open_stocks = [p['Stock'] for p in open_positions]
        for stock, df in stock_data.items():
            if stock in open_stocks: continue

            if stock in last_exit_dates:
                days_since_exit = (current_dt - last_exit_dates[stock]).days
                if days_since_exit < R['cooldown_days']:
                    debug_counter['cooldown'] += 1
                    continue

            if current_date not in df.index: continue

            i = df.index.get_loc(current_date)
            if i < 200: continue
            row = df.iloc[i]
            total_candles_checked += 1

            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_counter['liquidity'] += 1
                continue

            is_entry, rs_1m, rs_10d, high_10d = check_entry(df, i, current_date, debug_counter)
            if not is_entry:
                continue

            entry_price = row['Close']
            target_price = entry_price * (1 + R['fixed_target_pct']/100)
            sl_price = entry_price * (1 - R['fixed_sl_pct']/100)

            risk = entry_price - sl_price
            qty = int(R['risk_per_trade'] / risk) if risk > 0 else 0
            if qty == 0: continue

            open_positions.append({
                'Stock': stock, 'Entry_Date': current_date,
                'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                'Target': round(target_price, 2), 'RS_1M': rs_1m, 'RS_10D': rs_10d,
                '10D_High': round(high_10d, 2), 'Qty': qty
            })

    for pos in open_positions:
        df = stock_data[pos['Stock']]
        exit_price = df['Close'].iloc[-1]
        pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
        pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
        all_trades.append({
            'Stock': pos['Stock'],
            'Entry_Date': pos['Entry_Date'], 'Exit_Date': BACKTEST_END.strftime('%Y-%m-%d'),
            'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
            'Status': 'TIME', 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
            'Days_Held': (BACKTEST_END - pd.to_datetime(pos['Entry_Date']).date()).days,
            'RS_1M': pos['RS_1M'], 'RS_10D': pos['RS_10D'], '10D_High': pos['10D_High'], 'Qty': pos['Qty']
        })

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("DEBUG SUMMARY - VOLUME PATTERN", flush=True)
print("="*60, flush=True)
print(f"Total Candles Checked: {total_candles_checked}", flush=True)
for k, v in debug_counter.items():
    print(f"Rejected by {k}: {v}", flush=True)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS - VOLUME CONTRACTION + EXPANSION", flush=True)
print("="*60, flush=True)

if df_bt.empty:
    print("\nNo quality trades found with volume pattern rules.", flush=True)
else:
    total = len(df_bt)
    wins = len(df_bt[df_bt['Status'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_bt['PnL_Rs'].sum()
    win_amt = df_bt[df_bt['Status']=='WIN']['PnL_Rs'].sum()
    loss_amt = abs(df_bt[df_bt['Status']=='LOSS']['PnL_Rs'].sum())
    pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999

    print(f"\nTotal Trades: {total} | WR: {winrate}% | PF: {pf} | PnL: Rs.{total_pnl:,.0f}", flush=True)
    print(f"Avg RS: 1M={df_bt['RS_1M'].mean():.1f} | 10D={df_bt['RS_10D'].mean():.1f}", flush=True)

try:
    ws_bt = get_or_create_ws(sh, "RS_VOL_PATTERN_BT")
    ws_bt.clear()
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Saved to 'RS_VOL_PATTERN_BT' Sheet!", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)

print("\n=== COMPLETE ===", flush=True)
