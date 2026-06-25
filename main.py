import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

print("=== V21.5: Fill VIP Sheet + Batch 50 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

R = {
    'lookback_days': 10,
    'batch_size': 50,
    'max_workers': 5,
    'min_price': 20,
    'min_avg_volume': 100000,
    'min_daily_turnover': 1e7,
    'swing_window': 5,
    'base_range_max': 10.0,
    'volume_multiplier': 1.5,
    'candle_close_pos': 0.6,
    'candle_body_pct': 0.5,
    'min_winrate': 50.0,
    'min_trades_bt': 1,
    'rr_ratio': 2.0,
    'max_hold_days': 15,
    'vip_lock_days': 30 # 30 din ka lock
}

def get_or_create_ws(sh, title, rows=2000, cols=20):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_watchlist = get_or_create_ws(sh, "Watchlist")
ws_candidates = get_or_create_ws(sh, "PA_10D_CANDIDATES")
ws_backtest = get_or_create_ws(sh, "PA_HIST_BACKTEST")
ws_tradable = get_or_create_ws(sh, "TRADABLE_STOCKS")
ws_vip = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS") # V18.6 wali sheet

def get_watchlist_stocks():
    try:
        stocks = ws_watchlist.col_values(1)
        stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
        stocks = [s + '.NS' if not s.endswith('.NS') else s for s in stocks]
        if not stocks:
            return ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS"]
        print(f"Watchlist se {len(stocks)} stocks mile", flush=True)
        return stocks
    except:
        return ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS"]

def download_stock_data(ticker, start_date, end_date):
    try:
        df = yf.download(ticker, start=start_date, end=end_date + timedelta(days=1),
                         progress=False, auto_adjust=False, timeout=10)
        if df.empty or len(df) < 60: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if col in df.columns:
                df[col] = df[col].astype(float)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    except:
        return None

def find_swing_points(df):
    w = R['swing_window']
    df['Swing_High'] = df['High'][(df['High'].shift(w) < df['High']) &
                                  (df['High'].shift(-w) < df['High'])]
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    return df

def is_pure_price_action_signal(df, idx):
    if idx < 50: return False, "Not enough data", None
    row = df.iloc[idx]
    prev_row = df.iloc[idx-1]

    historical_swings = df['Swing_High'].iloc[:idx].dropna().tail(3)
    if len(historical_swings) < 2: return False, "No HH", None
    if historical_swings.iloc[-1] <= historical_swings.iloc[-2]: return False, "Not HH", None

    base_df = df.iloc[idx-10:idx]
    base_high = base_df['High'].max()
    base_low = base_df['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100
    if base_range_pct > R['base_range_max']: return False, f"Range {base_range_pct:.1f}%", None

    vol_20ma = row['Vol_20MA']
    if pd.isna(vol_20ma): return False, "No Vol MA", None
    cond_breakout = row['Close'] > base_high and prev_row['Close'] <= base_high
    cond_volume = row['Volume'] > vol_20ma * R['volume_multiplier']

    candle_range = row['High'] - row['Low']
    if candle_range == 0: return False, "Doji", None
    close_pos = (row['Close'] - row['Low']) / candle_range
    body_pct = abs(row['Close'] - row['Open']) / candle_range
    cond_candle = close_pos > R['candle_close_pos'] and body_pct > R['candle_body_pct']

    if not cond_breakout: return False, "No breakout", None
    if not cond_volume: return False, "Low volume", None
    if not cond_candle: return False, "Weak candle", None

    entry = row['Close']
    sl = base_low
    target = entry + R['rr_ratio'] * (entry - sl)
    signal_data = {'Entry': round(entry, 2), 'SL': round(sl, 2), 'Target': round(target, 2)}

    return True, f"HH+{base_range_pct:.1f}%Base", signal_data

def process_single_stock_pa(stock, start_date, end_date):
    try:
        df = download_stock_data(stock, start_date, end_date)
        if df is None: return None

        avg_vol = df['Volume'].tail(20).mean()
        avg_turnover = (df['Close'] * df['Volume']).tail(20).mean()
        if avg_vol < R['min_avg_volume']: return None
        if avg_turnover < R['min_daily_turnover']: return None
        if df['Close'].iloc[-1] < R['min_price']: return None

        df = find_swing_points(df)
        scan_start = max(50, len(df) - R['lookback_days'])

        for idx in range(scan_start, len(df)):
            is_signal, reason, signal_data = is_pure_price_action_signal(df, idx)
            if is_signal:
                return {
                    'Stock': stock.replace('.NS', ''),
                    'Signal_Date': df.index[idx].date().strftime('%Y-%m-%d'),
                    'Close': signal_data['Entry'],
                    'SL': signal_data['SL'],
                    'Target': signal_data['Target'],
                    'Volume': int(df['Volume'].iloc[idx]),
                    'Reason': reason
                }
        return None
    except:
        return None

def process_single_stock_backtest(stock, end_date):
    try:
        df = download_stock_data(f"{stock}.NS", end_date - timedelta(days=730), end_date)
        if df is None:
            return {'Stock': stock, 'Total_Trades': 0, 'Wins': 0, 'Losses': 0, 'WinRate': 0, 'Profit_Factor': 0, 'Status': 'No Data'}

        df = find_swing_points(df)
        trades = []
        in_trade = False
        entry = sl = tp = 0
        entry_idx = 0

        for i in range(50, len(df) - R['max_hold_days']):
            if not in_trade:
                is_signal, _, signal_data = is_pure_price_action_signal(df, i)
                if is_signal:
                    entry = signal_data['Entry']
                    sl = signal_data['SL']
                    tp = signal_data['Target']
                    if entry <= sl: continue
                    in_trade = True
                    entry_idx = i
            else:
                row = df.iloc[i]
                if row['Low'] <= sl:
                    trades.append(-1); in_trade = False
                elif row['High'] >= tp:
                    trades.append(1); in_trade = False
                elif i - entry_idx >= R['max_hold_days']:
                    pnl = (row['Close'] - entry) / entry * 100
                    trades.append(1 if pnl > 0 else -1); in_trade = False

        if len(trades) == 0:
            return {'Stock': stock, 'Total_Trades': 0, 'Wins': 0, 'Losses': 0, 'WinRate': 0, 'Profit_Factor': 0, 'Status': 'No PA Signal'}

        wins = sum(1 for t in trades if t > 0)
        winrate = wins / len(trades) * 100
        pf = wins / (len(trades) - wins) if len(trades) - wins > 0 else 99

        return {
            'Stock': stock,
            'Total_Trades': len(trades),
            'Wins': wins,
            'Losses': len(trades) - wins,
            'WinRate': round(winrate, 2),
            'Profit_Factor': round(pf, 2),
            'Status': 'OK' if len(trades) >= R['min_trades_bt'] else 'Low Trades'
        }
    except Exception as e:
        return {'Stock': stock, 'Total_Trades': 0, 'Wins': 0, 'Losses': 0, 'WinRate': 0, 'Profit_Factor': 0, 'Status': f'Error'}

def batch_process_pa(universe, end_date):
    candidates = []
    start_date = end_date - timedelta(days=R['lookback_days'] + 100)

    for i in range(0, len(universe), R['batch_size']):
        batch = universe[i:i+R['batch_size']]
        print(f"\nBatch {i//R['batch_size']+1}: Scanning {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(process_single_stock_pa, stock, start_date, end_date): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    candidates.append(result)
                    print(f"PA Found: {result['Stock']} on {result['Signal_Date']}", flush=True)

        time.sleep(1)

    return pd.DataFrame(candidates)

def batch_process_backtest(stock_list, end_date):
    results = []
    for i in range(0, len(stock_list), R['batch_size']):
        batch = stock_list[i:i+R['batch_size']]
        print(f"\nBacktest Batch {i//R['batch_size']+1}: {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(process_single_stock_backtest, stock, end_date): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
                    print(f"Backtested: {result['Stock']} WR:{result['WinRate']}% Trades:{result['Total_Trades']}", flush=True)

        time.sleep(1)

    return pd.DataFrame(results)

def main():
    today = datetime.now().date()
    print(f"\n=== V21.5 VIP Filler {today} ===", flush=True)

    stock_universe = get_watchlist_stocks()

    # PHASE 1
    print("\nPHASE 1: Scanning last 10 days PA...", flush=True)
    candidates_df = batch_process_pa(stock_universe, today)

    if candidates_df.empty:
        print(">>> No PA candidates found <<<", flush=True)
        ws_candidates.clear()
        ws_candidates.update('A1', [['Stock', 'Signal_Date', 'Close', 'Reason'], ['No signals', '', '', '']])
        ws_backtest.clear()
        ws_tradable.clear()
        ws_vip.clear()
        return

    sheet_candidates = candidates_df[['Stock', 'Signal_Date', 'Close', 'Volume', 'Reason']]
    ws_candidates.clear()
    ws_candidates.update('A1', [sheet_candidates.columns.tolist()] + sheet_candidates.values.tolist())

    # PHASE 2
    print(f"\nPHASE 2: Backtesting {len(candidates_df)} candidates...", flush=True)
    backtest_df = batch_process_backtest(candidates_df['Stock'].unique().tolist(), today)

    ws_backtest.clear()
    ws_backtest.update('A1', [backtest_df.columns.tolist()] + backtest_df.values.tolist())

    # PHASE 3: TRADABLE + VIP SHEET UPDATE
    tradable = backtest_df[(backtest_df['WinRate'] >= R['min_winrate']) & (backtest_df['Total_Trades'] >= R['min_trades_bt'])].copy()
    print(f"\nPHASE 3: {len(tradable)} stocks with WR >= {R['min_winrate']}%", flush=True)

    tradable_final = []
    vip_stocks_list = []

    for _, row in tradable.iterrows():
        stock_name = row['Stock']
        match = candidates_df[candidates_df['Stock'] == stock_name].iloc[-1]
        vip_stocks_list.append(stock_name) # VIP list me add kar

        tradable_final.append({
            'Breakout_Date': match['Signal_Date'],
            'Stock': stock_name,
            'Entry_Price': match['Close'],
            'StopLoss_Price': match['SL'],
            'Target_Price': match['Target'],
            'Backtest_WR': row['WinRate'],
            'Total_Trades': row['Total_Trades']
        })

    tradable_df = pd.DataFrame(tradable_final)
    ws_tradable.clear()
    if tradable_df.empty:
        ws_tradable.update('A1', [['Breakout_Date', 'Stock', 'Entry_Price', 'StopLoss_Price', 'Target_Price', 'Backtest_WR', 'Total_Trades'],
                                  ['No tradable', '', '', '', '', '', '']])
    else:
        ws_tradable.update('A1', [tradable_df.columns.tolist()] + tradable_df.values.tolist())

    # 🎯 YAHI MAIN FIX: HIGH_WINRATE_STOCKS BHAR DO
    ws_vip.clear()
    lock_date = today + timedelta(days=R['vip_lock_days'])
    vip_data = [[f"LOCK_UNTIL: {lock_date.strftime('%Y-%m-%d')}"]]
    vip_data.append([]) # Empty row
    vip_data.extend([[stock] for stock in sorted(vip_stocks_list)])

    if vip_stocks_list:
        ws_vip.update('A1', vip_data)
        print(f"\n=== VIP SHEET UPDATED: {len(vip_stocks_list)} stocks locked till {lock_date} ===", flush=True)
    else:
        ws_vip.update('A1', [['LOCK_UNTIL: 2026-01-01'], [], ['No VIP stocks found']])
        print(f"\n=== VIP SHEET EMPTY: No stocks passed filter ===", flush=True)

    print(f"\n=== V21.5 COMPLETE: {len(tradable_df)} Tradable Stocks ===", flush=True)

if __name__ == "__main__":
    main()
