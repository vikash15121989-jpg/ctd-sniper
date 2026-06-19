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
BACKTEST_START = BACKTEST_END - timedelta(days=365) # 1 SAAL BACKTEST
BATCH_SIZE = 50

print("=== RS BEATER V14.1 - 6% SNIPER FINAL ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 6% SNIPER RULES - FIXED TARGET
R = {
    'min_daily_value_cr': 50.0, # 50Cr liquidity - Sirf Nifty100 level
    'fixed_target_pct': 6.0, # EXACT 6% TARGET
    'fixed_sl_pct': 3.0, # EXACT 3% SL = RR 1:2
    'momentum_5d_min': 8.0, # 5 din me 8%+ chalna chahiye
    'momentum_5d_max': 12.0, # 12% se zyada = Overextended
    'pullback_pct': 2.0, # 5D high se 2% pullback pe entry
    'rsi_min': 62,
    'rsi_max': 72,
    'rs_1m_min': 8.0, # Nifty se 1 mahine me 8%+ aage
    'vol_ratio_min': 2.0, # 2x volume minimum
    'time_stop_days': 6, # 6 din me exit max
    'risk_per_trade': 3000, # 3k risk per trade
    'cooldown_days': 15, # 15 din cooldown
    'max_open_trades': 2, # Max 2 position
    'min_base_days': 10, # 10 din ka tight base chahiye
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    df['Ret_5D'] = df['Close'].pct_change(5) * 100
    df['High_5D'] = df['High'].rolling(5).max()
    return df

print("\n Downloading Nifty 50 reference data...", flush=True)
nifty_df = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=100), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex): nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_df.index = pd.to_datetime(nifty_df.index).strftime('%Y-%m-%d')
nifty_df = nifty_df[~nifty_df.index.duplicated(keep='last')]

def calculate_rs_1m(df, i, current_date, nifty_df):
    try:
        if current_date not in nifty_df.index or i < 21: return None
        nifty_idx = nifty_df.index.get_loc(current_date)
        if nifty_idx < 21: return None

        stock_start = df['Close'].iloc[i-21]
        stock_now = df['Close'].iloc[i]
        stock_ret = ((stock_now - stock_start) / stock_start) * 100

        nifty_start = nifty_df['Close'].iloc[nifty_idx-21]
        nifty_now = nifty_df['Close'].iloc[nifty_idx]
        nifty_ret = ((nifty_now - nifty_start) / nifty_start) * 100

        return round(stock_ret - nifty_ret, 2)
    except:
        return None

def check_entry(df, i, current_date, debug_counter):
    row = df.iloc[i]
    if pd.isna(row['EMA50']) or pd.isna(row['RSI']) or pd.isna(row['Ret_5D']):
        debug_counter['nan'] += 1
        return False, 0, 0

    # 1. UPTREND: Close > EMA20 > EMA50
    trend = row['Close'] > row['EMA20'] > row['EMA50']
    if not trend:
        debug_counter['trend'] += 1
        return False, 0, 0

    # 2. BASE CHECK: Pichle 10 din me 12% se zyada range nahi
    if i < 10: return False, 0, 0
    high_10d = df['High'].iloc[i-10:i].max()
    low_10d = df['Low'].iloc[i-10:i].min()
    base_range = (high_10d - low_10d) / low_10d
    if base_range > 0.12:
        debug_counter['no_base'] += 1
        return False, 0, 0

    # 3. RSI 62-72: Strong momentum zone
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    if not rsi_ok:
        debug_counter['rsi'] += 1
        return False, 0, 0

    # 4. MOMENTUM BURST: 5 din me 8-12%
    mom_5d = row['Ret_5D']
    if not (R['momentum_5d_min'] <= mom_5d <= R['momentum_5d_max']):
        debug_counter['momentum'] += 1
        return False, 0, 0

    # 5. PULLBACK ENTRY: 5D high se 2% pullback
    high_5d = row['High_5D']
    pullback_from_high = ((high_5d - row['Close']) / high_5d) * 100
    if pullback_from_high < R['pullback_pct'] or pullback_from_high > 4.0:
        debug_counter['no_pullback'] += 1
        return False, 0, 0

    # 6. RS 1M > 8%: Market leader
    rs_1m = calculate_rs_1m(df, i, current_date, nifty_df)
    if rs_1m is None or rs_1m < R['rs_1m_min']:
        debug_counter['rs_weak'] += 1
        return False, 0, 0

    # 7. VOLUME: 2x minimum
    if row['Vol_MA20'] < 1000 or row['Volume'] < (row['Vol_MA20'] * R['vol_ratio_min']):
        debug_counter['volume'] += 1
        return False, 0, 0

    return True, rs_1m, mom_5d

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=100),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 50: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_indicators(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
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

debug_counter = {'nan':0, 'trend':0, 'no_base':0, 'rsi':0, 'momentum':0, 'no_pullback':0,
                'rs_weak':0, 'volume':0, 'liquidity':0, 'cooldown':0, 'max_positions':0}
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

            if sl_hit:
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
                    'Days_Held': days_held, 'RS_1M': pos['RS_1M'],
                    'Mom_5D': pos['Mom_5D'], 'Qty': pos['Qty']
                })
                last_exit_dates[pos['Stock']] = current_dt
                open_positions.remove(pos)

        if len(open_positions) >= R['max_open_trades']:
            debug_counter['max_positions'] += 1
            continue

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
            if i < 50: continue
            row = df.iloc[i]
            total_candles_checked += 1

            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_counter['liquidity'] += 1
                continue

            is_entry, rs_1m, mom_5d = check_entry(df, i, current_date, debug_counter)
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
                'Target': round(target_price, 2), 'RS_1M': rs_1m,
                'Mom_5D': mom_5d, 'Qty': qty
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
            'RS_1M': pos['RS_1M'], 'Mom_5D': pos['Mom_5D'], 'Qty': pos['Qty']
        })

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("DEBUG SUMMARY - 6% SNIPER V14.1", flush=True)
print("="*60, flush=True)
print(f"Total Candles Checked: {total_candles_checked}", flush=True)
for k, v in debug_counter.items():
    print(f"Rejected by {k}: {v}", flush=True)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS - 6% SNIPER V14.1", flush=True)
print("="*60, flush=True)

if df_bt.empty:
    print("\nNo trades found. Filters bahut tight hain.", flush=True)
else:
    total = len(df_bt)
    wins = len(df_bt[df_bt['Status'] == 'WIN'])
    winrate = round(wins / total * 100, 1) if total else 0
    total_pnl = df_bt['PnL_Rs'].sum()
    win_amt = df_bt[df_bt['Status']=='WIN']['PnL_Rs'].sum()
    loss_amt = abs(df_bt[df_bt['Status']=='LOSS']['PnL_Rs'].sum())
    pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999
    avg_days = round(df_bt['Days_Held'].mean(), 1)

    win_trades = df_bt[df_bt['Status'] == 'WIN']
    avg_win = round(win_trades['PnL_%'].mean(), 1) if len(win_trades) > 0 else 0

    loss_trades = df_bt[df_bt['Status'] == 'LOSS']
    avg_loss = round(loss_trades['PnL_%'].mean(), 1) if len(loss_trades) > 0 else 0

    print(f"\nTotal Trades: {total} | WR: {winrate}% | PF: {pf} | PnL: Rs.{total_pnl:,.0f}", flush=True)
    print(f"Avg Win: {avg_win}% | Avg Loss: {avg_loss}% | Avg Days: {avg_days}", flush=True)
    print(f"Avg RS_1M: {df_bt['RS_1M'].mean():.1f}% | Avg 5D Mom: {df_bt['Mom_5D'].mean():.1f}%", flush=True)

    trades_per_month = round(total / 12, 1)
    print(f"Trades/Month: {trades_per_month} | Avg Gap: ~{round(30/trades_per_month, 0)} days", flush=True)

try:
    ws_bt = get_or_create_ws(sh, "6PCT_SNIPER_BT")
    ws_bt.clear()
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Saved to '6PCT_SNIPER_BT' Sheet!", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)

print("\n=== COMPLETE ===", flush=True)
