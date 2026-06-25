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

print("=== V21.6: GHOST PROTOCOL SCANNER ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

# ===== GHOST PROTOCOL CONFIG =====
R = {
    'lookback_days': 15, # Signal naya hai to 15 din hi scan
    'batch_size': 50,
    'max_workers': 5,
    'min_price': 60, # Penny stock nahi
    'min_avg_volume': 100000,
    'min_daily_turnover': 1e7,
    # GHOST SPECIFIC
    'shelf_days': 40, # 40 din ka volume shelf
    'shelf_range_max': 0.18, # 18% se tight range
    'shelf_vol_dry_pct': 0.70, # 70% din volume dry hona chahiye
    'pivot_vol_multiple': 1.8, # Pocket pivot me 1.8x volume
    'down_vol_dry': 0.6, # Girne wale din 60% se kam volume
    'rs_lookback': 50, # RS line 50 din
    # BACKTEST
    'min_winrate': 28.0, # 28% WR chalega 1:6 RR me
    'min_trades_bt': 3, # Kam se kam 3 trade 2 saal me
    'rr_ratio': 6.0, # 1:6 RR
    'max_hold_days': 75, # 75 din hold
    'vip_lock_days': 30
}

def get_or_create_ws(sh, title, rows=2000, cols=20):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_watchlist = get_or_create_ws(sh, "Watchlist")
ws_candidates = get_or_create_ws(sh, "GHOST_CANDIDATES")
ws_backtest = get_or_create_ws(sh, "GHOST_BACKTEST")
ws_tradable = get_or_create_ws(sh, "GHOST_TRADABLE")
ws_vip = get_or_create_ws(sh, "GHOST_VIP")

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
        if df.empty or len(df) < 100: return None # 100 din chahiye shelf ke liye
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

# ===== NIFTY DATA FOR RS CALCULATION =====
def get_nifty_data(start_date, end_date):
    try:
        nifty = yf.download("^NSEI", start=start_date, end=end_date + timedelta(days=1), progress=False)
        if nifty.empty: return None
        return nifty['Close']
    except:
        return None

def build_ghost_indicators(df, nifty_close):
    df['Vol_50MA'] = df['Volume'].rolling(window=50).mean()
    df['High_40D'] = df['High'].rolling(window=40).max()
    df['Low_40D'] = df['Low'].rolling(window=40).min()

    # RS Line
    df = df.join(nifty_close.rename('NIFTY'), how='left')
    df['RS_Line'] = df['Close'] / df['NIFTY']
    df['RS_High_50D'] = df['RS_Line'].rolling(window=R['rs_lookback']).max()
    return df

# ===== GHOST PROTOCOL SIGNAL CHECK =====
def is_ghost_protocol_signal(df, idx):
    if idx < 60: return False, "Not enough data", None
    row = df.iloc[idx]
    shelf_df = df.iloc[idx-R['shelf_days']:idx]

    # Shart 1: Volume Shelf
    dry_vol_days = (shelf_df['Volume'] < shelf_df['Vol_50MA'] * 0.6).sum()
    shelf_range = (shelf_df['High'].max() - shelf_df['Low'].min()) / shelf_df['Low'].min()
    if dry_vol_days < R['shelf_days'] * R['shelf_vol_dry_pct']: return False, "No Shelf", None
    if shelf_range > R['shelf_range_max']: return False, f"Range {shelf_range*100:.1f}%", None

    # Shart 2: Down Volume Dry
    down_days = shelf_df[shelf_df['Close'] < shelf_df['Open']]
    if len(down_days) > 3:
        avg_down_vol = down_days['Volume'].mean()
        if avg_down_vol > row['Vol_50MA'] * R['down_vol_dry']: return False, "Down Vol High", None

    # Shart 3: Pocket Pivot Day
    is_green = row['Close'] > row['Open']
    near_high = row['Close'] >= row['High_40D'] * 0.98
    vol_explosion = row['Volume'] > row['Vol_50MA'] * R['pivot_vol_multiple']
    highest_vol_10d = row['Volume'] == shelf_df['Volume'].tail(10).max()
    if not (is_green and near_high and vol_explosion and highest_vol_10d):
        return False, "No Pivot", None

    # Shart 4: RS Line New High
    if pd.isna(row['RS_Line']) or pd.isna(row['RS_High_50D']): return False, "No RS", None
    if row['RS_Line'] < row['RS_High_50D'] * 0.99: return False, "RS Weak", None

    entry = row['Close']
    sl = row['Low_40D'] * 0.98 # 40D low ke 2% neeche SL
    target = entry + R['rr_ratio'] * (entry - sl)
    signal_data = {'Entry': round(entry, 2), 'SL': round(sl, 2), 'Target': round(target, 2)}

    return True, f"Ghost 4/4", signal_data

def process_single_stock_ghost(stock, start_date, end_date, nifty_close):
    try:
        df = download_stock_data(stock, start_date, end_date)
        if df is None: return None

        avg_vol = df['Volume'].tail(20).mean()
        avg_turnover = (df['Close'] * df['Volume']).tail(20).mean()
        if avg_vol < R['min_avg_volume']: return None
        if avg_turnover < R['min_daily_turnover']: return None
        if df['Close'].iloc[-1] < R['min_price']: return None

        df = build_ghost_indicators(df, nifty_close)
        scan_start = max(60, len(df) - R['lookback_days'])

        for idx in range(scan_start, len(df)):
            is_signal, reason, signal_data = is_ghost_protocol_signal(df, idx)
            if is_signal:
                return {
                    'Stock': stock.replace('.NS', ''),
                    'Signal_Date': df.index[idx].date().strftime('%Y-%m-%d'),
                    'Entry': signal_data['Entry'],
                    'SL': signal_data['SL'],
                    'Target': signal_data['Target'],
                    'Volume': int(df['Volume'].iloc[idx]),
                    'Reason': reason
                }
        return None
    except:
        return None

def process_single_stock_backtest_ghost(stock, end_date, nifty_close):
    try:
        df = download_stock_data(f"{stock}.NS", end_date - timedelta(days=730), end_date)
        if df is None:
            return {'Stock': stock, 'Total_Trades': 0, 'Wins': 0, 'Losses': 0, 'WinRate': 0, 'Profit_Factor': 0, 'Status': 'No Data'}

        df = build_ghost_indicators(df, nifty_close)
        trades = []
        in_trade = False
        entry = sl = tp = 0
        entry_idx = 0

        for i in range(60, len(df) - R['max_hold_days']):
            if not in_trade:
                is_signal, _, signal_data = is_ghost_protocol_signal(df, i)
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
            return {'Stock': stock, 'Total_Trades': 0, 'Wins': 0, 'Losses': 0, 'WinRate': 0, 'Profit_Factor': 0, 'Status': 'No Ghost Signal'}

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

def batch_process_ghost(universe, end_date, nifty_close):
    candidates = []
    start_date = end_date - timedelta(days=R['lookback_days'] + 100)

    for i in range(0, len(universe), R['batch_size']):
        batch = universe[i:i+R['batch_size']]
        print(f"\nGhost Batch {i//R['batch_size']+1}: Scanning {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(process_single_stock_ghost, stock, start_date, end_date, nifty_close): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    candidates.append(result)
                    print(f"GHOST FOUND: {result['Stock']} on {result['Signal_Date']} @ {result['Entry']}", flush=True)
        time.sleep(1)
    return pd.DataFrame(candidates)

def batch_process_backtest_ghost(stock_list, end_date, nifty_close):
    results = []
    for i in range(0, len(stock_list), R['batch_size']):
        batch = stock_list[i:i+R['batch_size']]
        print(f"\nGhost Backtest Batch {i//R['batch_size']+1}: {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(process_single_stock_backtest_ghost, stock, end_date, nifty_close): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
                    print(f"Backtested: {result['Stock']} WR:{result['WinRate']}% Trades:{result['Total_Trades']}", flush=True)
        time.sleep(1)
    return pd.DataFrame(results)

def main():
    today = datetime.now().date()
    print(f"\n=== V21.6 GHOST PROTOCOL {today} ===", flush=True)

    stock_universe = get_watchlist_stocks()

    # NIFTY data ek baar download karo RS ke liye
    nifty_close = get_nifty_data(today - timedelta(days=800), today)
    if nifty_close is None:
        print("NIFTY data nahi mila. Exit.", flush=True)
        return

    # PHASE 1: GHOST SCAN
    print("\nPHASE 1: Scanning for Ghost Protocol...", flush=True)
    candidates_df = batch_process_ghost(stock_universe, today, nifty_close)

    if candidates_df.empty:
        print(">>> No Ghost candidates found <<<", flush=True)
        ws_candidates.clear()
        ws_candidates.update('A1', [['Stock', 'Signal_Date', 'Entry', 'Reason'], ['No ghosts', '', '', '']])
        ws_backtest.clear(); ws_tradable.clear(); ws_vip.clear()
        return

    ws_candidates.clear()
    ws_candidates.update('A1', [candidates_df.columns.tolist()] + candidates_df.values.tolist())

    # PHASE 2: BACKTEST
    print(f"\nPHASE 2: Backtesting {len(candidates_df)} ghosts...", flush=True)
    backtest_df = batch_process_backtest_ghost(candidates_df['Stock'].unique().tolist(), today, nifty_close)

    ws_backtest.clear()
    ws_backtest.update('A1', [backtest_df.columns.tolist()] + backtest_df.values.tolist())

    # PHASE 3: TRADABLE + VIP
    tradable = backtest_df[(backtest_df['WinRate'] >= R['min_winrate']) & (backtest_df['Total_Trades'] >= R['min_trades_bt'])].copy()
    print(f"\nPHASE 3: {len(tradable)} ghosts with WR >= {R['min_winrate']}%", flush=True)

    tradable_final = []
    vip_stocks_list = []

    for _, row in tradable.iterrows():
        stock_name = row['Stock']
        match = candidates_df[candidates_df['Stock'] == stock_name].iloc[-1]
        vip_stocks_list.append(stock_name)

        tradable_final.append({
            'Breakout_Date': match['Signal_Date'],
            'Stock': stock_name,
            'Entry_Price': match['Entry'],
            'StopLoss_Price': match['SL'],
            'Target_Price': match['Target'],
            'Backtest_WR': row['WinRate'],
            'Total_Trades': row['Total_Trades'],
            'RR': R['rr_ratio']
        })

    tradable_df = pd.DataFrame(tradable_final)
    ws_tradable.clear()
    if tradable_df.empty:
        ws_tradable.update('A1', [['No tradable ghosts', '', '', '', '', '', '', '']])
    else:
        ws_tradable.update('A1', [tradable_df.columns.tolist()] + tradable_df.values.tolist())

    # VIP SHEET UPDATE
    ws_vip.clear()
    lock_date = today + timedelta(days=R['vip_lock_days'])
    vip_data = [[f"GHOST_LOCK_UNTIL: {lock_date.strftime('%Y-%m-%d')}"]]
    vip_data.append(['Stock', 'RR', 'Note'])
    vip_data.extend([[stock, R['rr_ratio'], 'Ghost 4/4'] for stock in sorted(vip_stocks_list)])

    if vip_stocks_list:
        ws_vip.update('A1', vip_data)
        print(f"\n=== GHOST VIP UPDATED: {len(vip_stocks_list)} stocks locked till {lock_date} ===", flush=True)
    else:
        ws_vip.update('A1', [['No Ghost VIP stocks found']])
        print(f"\n=== GHOST VIP EMPTY ===", flush=True)

    print(f"\n=== V21.6 COMPLETE: {len(tradable_df)} Ghost Tradable ===", flush=True)

if __name__ == "__main__":
    main()
