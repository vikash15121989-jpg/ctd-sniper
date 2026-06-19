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
BACKTEST_START = BACKTEST_END - timedelta(days=730) # 2 SAAL
BATCH_SIZE = 50

print("=== RS BEATER V11 - NIFTY TOP SE BEAT SWING ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# SWING RULES - NIFTY TOP SE BEAT LOGIC
R = {
    'min_daily_value_cr': 10.0, # Sirf Nifty50 - Blue chip
    'fixed_target_pct': 8.0, # 8% TARGET - Swing me milta hai
    'fixed_sl_pct': 3.5, # 3.5% SL = RR 1:2.28
    'rsi_min': 60,
    'rsi_max': 75,
    'min_rs_from_top': 8.0, # NIFTY TOP SE 8%+ BEAT - Key Filter
    'breakout_lookback': 60, # 60D = QUARTERLY HIGH
    'breakout_buffer_pct': 1.0, # 60D high ke 1% upar
    'vol_blast_ratio': 2.5, # 2.5x Volume - Institutional
    'time_stop_days': 20, # 20 din hold - Pure swing
    'risk_per_trade': 3000, # 3k risk per trade
    'cooldown_days': 45, # 45 din cooldown - Ek stock 2 mahine me 1 baar
    'max_open_trades': 2 # Max 2 position - Overtrade nahi
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['Vol_MA50'] = df['Volume'].rolling(window=50).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

print("\n Downloading Nifty 50 reference data...", flush=True)
nifty_df = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex): nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_df.index = pd.to_datetime(nifty_df.index).strftime('%Y-%m-%d')
nifty_df = nifty_df[~nifty_df.index.duplicated(keep='last')]

# NIFTY 52W HIGH DATE NIKALO - DYNAMIC
def get_nifty_top_date(current_date, nifty_df):
    """Current date se pehle 252 days me Nifty ka top kab tha"""
    current_idx = nifty_df.index.get_loc(current_date)
    if current_idx < 252: return None, None
    nifty_52w = nifty_df.iloc[current_idx-252:current_idx]
    nifty_top_idx = nifty_52w['High'].idxmax()
    nifty_top_close = nifty_52w.loc[nifty_top_idx, 'Close']
    return nifty_top_idx, nifty_top_close

def calculate_rs_from_nifty_top(df, i, current_date, nifty_df):
    """Nifty top se aaj tak ka RS"""
    try:
        nifty_top_date, nifty_top_close = get_nifty_top_date(current_date, nifty_df)
        if nifty_top_date is None: return None, None, None

        # Stock ka price nifty top wale din
        if nifty_top_date not in df.index:
            available_dates = df.index[df.index >= nifty_top_date]
            if len(available_dates) == 0: return None, None, None
            stock_date = available_dates[0]
        else:
            stock_date = nifty_top_date

        stock_then = df.loc[stock_date, 'Close']
        stock_now = df['Close'].iloc[i]
        stock_ret = ((stock_now - stock_then) / stock_then) * 100

        nifty_now = nifty_df.loc[current_date, 'Close']
        nifty_ret = ((nifty_now - nifty_top_close) / nifty_top_close) * 100

        rs_from_top = round(stock_ret - nifty_ret, 2)
        return rs_from_top, stock_ret, nifty_ret
    except:
        return None, None, None

def check_entry(df, i, current_date, debug_counter):
    row = df.iloc[i]
    if pd.isna(row['EMA200']) or pd.isna(row['RSI']) or pd.isna(row['Vol_MA50']):
        debug_counter['nan'] += 1
        return False, 0, 0, 0, 0

    # 1. MAJOR UPTREND - 200 EMA ke upar
    trend = row['Close'] > row['EMA50'] > row['EMA200']
    if not trend:
        debug_counter['trend'] += 1
        return False, 0, 0, 0, 0

    # 2. RSI 60-75
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    if not rsi_ok:
        debug_counter['rsi'] += 1
        return False, 0, 0, 0, 0

    # 3. KEY LOGIC: NIFTY TOP SE RS > +8%
    rs_from_top, stock_ret, nifty_ret = calculate_rs_from_nifty_top(df, i, current_date, nifty_df)
    if rs_from_top is None:
        debug_counter['rs_error'] += 1
        return False, 0, 0, 0, 0

    if rs_from_top < R['min_rs_from_top']:
        debug_counter['rs_weak_from_top'] += 1
        return False, 0, 0, 0, 0

    # 4. 60D QUARTERLY BREAKOUT
    if i < R['breakout_lookback']:
        return False, 0, 0, 0, 0
    lookback_df = df.iloc[i-R['breakout_lookback']:i]
    max_high_60d = lookback_df['High'].max()

    breakout_level = max_high_60d * (1 + R['breakout_buffer_pct']/100)
    if row['Close'] <= breakout_level:
        debug_counter['no_breakout'] += 1
        return False, 0, 0, 0, 0

    # 5. INSTITUTIONAL VOLUME - 2.5x
    if row['Vol_MA50'] < 1000 or row['Volume'] < (row['Vol_MA50'] * R['vol_blast_ratio']):
        debug_counter['volume'] += 1
        return False, 0, 0, 0, 0

    return True, rs_from_top, stock_ret, nifty_ret, max_high_60d

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=400),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 250: return None, stock
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

debug_counter = {'nan':0, 'trend':0, 'rsi':0, 'rs_weak_from_top':0, 'no_breakout':0, 'volume':0, 'liquidity':0, 'rs_error':0, 'cooldown':0, 'max_positions':0}
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
                    'Days_Held': days_held, 'RS_From_Top': pos['RS_From_Top'],
                    'Stock_Ret': pos['Stock_Ret'], 'Nifty_Ret': pos['Nifty_Ret'],
                    '60D_High': pos['60D_High'], 'Qty': pos['Qty']
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
            if i < 250: continue
            row = df.iloc[i]
            total_candles_checked += 1

            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_counter['liquidity'] += 1
                continue

            is_entry, rs_from_top, stock_ret, nifty_ret, high_60d = check_entry(df, i, current_date, debug_counter)
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
                'Target': round(target_price, 2), 'RS_From_Top': rs_from_top,
                'Stock_Ret': stock_ret, 'Nifty_Ret': nifty_ret,
                '60D_High': round(high_60d, 2), 'Qty': qty
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
            'RS_From_Top': pos['RS_From_Top'], 'Stock_Ret': pos['Stock_Ret'],
            'Nifty_Ret': pos['Nifty_Ret'], '60D_High': pos['60D_High'], 'Qty': pos['Qty']
        })

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("DEBUG SUMMARY - NIFTY TOP SWING", flush=True)
print("="*60, flush=True)
print(f"Total Candles Checked: {total_candles_checked}", flush=True)
for k, v in debug_counter.items():
    print(f"Rejected by {k}: {v}", flush=True)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS - NIFTY TOP SWING", flush=True)
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

    print(f"\nTotal Trades: {total} | WR: {winrate}% | PF: {pf} | PnL: Rs.{total_pnl:,.0f}", flush=True)
    print(f"Avg Days Held: {avg_days} | Avg RS_From_Top: {df_bt['RS_From_Top'].mean():.1f}%", flush=True)

    trades_per_year = round(total / 2, 1)
    trades_per_month = round(total / 24, 1)
    print(f"Trades/Year: {trades_per_year} | Trades/Month: {trades_per_month}", flush=True)
    print(f"Avg Gap: ~{round(365/trades_per_year, 0)} days between trades", flush=True)

try:
    ws_bt = get_or_create_ws(sh, "NIFTY_TOP_SWING_BT")
    ws_bt.clear()
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Saved to 'NIFTY_TOP_SWING_BT' Sheet!", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)

print("\n=== COMPLETE ===", flush=True)
