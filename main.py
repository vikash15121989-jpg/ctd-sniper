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

print("=== V21.7 GHOST: FULL SCAN FIXED ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== GOOGLE SHEET SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

# ===== GHOST FINAL CONFIG - BUG FIXED =====
R = {
    'backtest_start': '2026-01-01', # Jan se June 2026
    'backtest_end': '2026-06-30', # 9 June cover ho jayega
    'batch_size': 50,
    'max_workers': 10,

    # FILTER RELAXED - kachra bhi check hoga
    'min_price': 50, # 60 se 50
    'min_avg_volume': 50000, # 1L se 50k
    'min_daily_turnover': 2e6, # 1Cr se 20Lakh

    # GHOST LOGIC RELAXED - 9 June pakadne ke liye
    'shelf_days': 30, # 35 se 30
    'shelf_range_max': 0.22, # 20% se 22%
    'shelf_vol_dry_pct': 0.60, # 65% se 60%
    'pivot_vol_multiple': 1.3, # 1.5 se 1.3
    'down_vol_dry': 0.75, # 0.7 se 0.75
    'rs_lookback': 50,
    'min_ghost_score': 2, # 4 me se 2 shart = signal
    'rr_ratio': 3.0, # 4 se 3
    'max_hold_days': 60,
}

def get_or_create_ws(sh, title, rows=5000, cols=20):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_watchlist = get_or_create_ws(sh, "Watchlist")
ws_bt_signals = get_or_create_ws(sh, "GHOST_JAN_JUNE_2026")

def get_watchlist_stocks():
    try:
        stocks = ws_watchlist.col_values(1)
        stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
        stocks = [s + '.NS' if not s.endswith('.NS') else s for s in stocks]
        print(f"Watchlist Loaded: {len(stocks)} stocks", flush=True)
        return stocks if stocks else ["RELIANCE.NS", "TCS.NS"]
    except:
        return ["RELIANCE.NS", "TCS.NS"]

def download_stock_data(ticker, start_date, end_date):
    try:
        # 400 din pehle ka data chahiye shelf ke liye
        df = yf.download(ticker, start=start_date - timedelta(days=400), end=end_date + timedelta(days=1),
                         progress=False, auto_adjust=False, timeout=15)
        if df.empty or len(df) < 100: return None
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

def get_nifty_data(start_date, end_date):
    try:
        nifty = yf.download("^NSEI", start=start_date - timedelta(days=400), end=end_date + timedelta(days=1), progress=False)
        return nifty['Close'] if not nifty.empty else None
    except:
        return None

def build_ghost_indicators(df, nifty_close):
    df['Vol_50MA'] = df['Volume'].rolling(window=50).mean()
    df['High_40D'] = df['High'].rolling(window=40).max()
    df['Low_40D'] = df['Low'].rolling(window=40).min()
    df = df.join(nifty_close.rename('NIFTY'), how='left')
    df['RS_Line'] = df['Close'] / df['NIFTY']
    df['RS_High_50D'] = df['RS_Line'].rolling(window=R['rs_lookback']).max()
    return df

def is_ghost_protocol_signal(df, idx):
    if idx < 60: return False, 0, None
    row = df.iloc[idx]
    shelf_df = df.iloc[idx-R['shelf_days']:idx]
    score = 0
    reasons = []

    # Shart 1: Shelf + Dry Volume
    dry_vol_days = (shelf_df['Volume'] < shelf_df['Vol_50MA'] * 0.7).sum()
    shelf_range = (shelf_df['High'].max() - shelf_df['Low'].min()) / shelf_df['Low'].min()
    if dry_vol_days >= R['shelf_days'] * R['shelf_vol_dry_pct'] and shelf_range <= R['shelf_range_max']:
        score += 2; reasons.append("Shelf")

    # Shart 2: Down Days Dry
    down_days = shelf_df[shelf_df['Close'] < shelf_df['Open']]
    if len(down_days) > 3:
        avg_down_vol = down_days['Volume'].mean()
        if avg_down_vol < row['Vol_50MA'] * R['down_vol_dry']:
            score += 1; reasons.append("Dry")

    # Shart 3: Pivot Day
    is_green = row['Close'] > row['Open'] * 1.005 # 0.5% green
    near_high = row['Close'] >= row['High_40D'] * 0.97
    vol_explosion = row['Volume'] > row['Vol_50MA'] * R['pivot_vol_multiple']
    if is_green and near_high and vol_explosion:
        score += 2; reasons.append("Pivot")

    # Shart 4: RS New High
    if not pd.isna(row['RS_Line']) and not pd.isna(row['RS_High_50D']):
        if row['RS_Line'] >= row['RS_High_50D'] * 0.98:
            score += 1; reasons.append("RS")

    if score >= R['min_ghost_score']:
        entry = row['Close']
        sl = row['Low_40D'] * 0.98
        target = entry + R['rr_ratio'] * (entry - sl)
        return True, score, {'Entry': round(entry, 2), 'SL': round(sl, 2), 'Target': round(target, 2), 'Reason': "+".join(reasons)}

    return False, score, None

def backtest_stock_full_year(stock, start_date, end_date, nifty_close):
    try:
        df = download_stock_data(stock, start_date, end_date)
        if df is None:
            print(f"NO DATA: {stock}", flush=True)
            return []

        # FILTER CHECK - ab baad me karenge, pehle data check
        avg_price = df['Close'].tail(20).mean()
        avg_vol = df['Volume'].tail(20).mean()
        avg_turnover = (df['Close'] * df['Volume']).tail(20).mean()

        if avg_price < R['min_price']: return []
        if avg_vol < R['min_avg_volume']: return []
        if avg_turnover < R['min_daily_turnover']: return []

        df = build_ghost_indicators(df, nifty_close)
        signals = []

        # Sirf Jan-June 2026 ke andar scan karo
        df_scan = df[(df.index >= start_date) & (df.index <= end_date)]
        if df_scan.empty: return []

        start_idx = df.index.get_loc(df_scan.index[0])

        for idx in range(start_idx, len(df)):
            is_signal, score, signal_data = is_ghost_protocol_signal(df, idx)
            if is_signal:
                sig_date = df.index[idx].date().strftime('%Y-%m-%d')
                signals.append({
                    'Date': sig_date,
                    'Stock': stock.replace('.NS', ''),
                    'Entry': signal_data['Entry'],
                    'SL': signal_data['SL'],
                    'Target': signal_data['Target'],
                    'Score': score,
                    'Reason': signal_data['Reason']
                })
                print(f"GHOST FOUND: {stock.replace('.NS','')} on {sig_date} @ {signal_data['Entry']} Score:{score}/6", flush=True)
        return signals
    except Exception as e:
        return []

def main():
    start_date = pd.to_datetime(R['backtest_start']).date()
    end_date = pd.to_datetime(R['backtest_end']).date()
    print(f"\n=== SCANNING {start_date} to {end_date} ===", flush=True)
    print(f"Filters: Price>{R['min_price']} Vol>{R['min_avg_volume']} Score>={R['min_ghost_score']}/6", flush=True)

    stock_universe = get_watchlist_stocks()
    nifty_close = get_nifty_data(start_date, end_date)
    if nifty_close is None:
        print("NIFTY data failed. Exit.", flush=True)
        return

    all_signals = []

    for i in range(0, len(stock_universe), R['batch_size']):
        batch = stock_universe[i:i+R['batch_size']]
        print(f"\nBatch {i//R['batch_size']+1}: Scanning {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(backtest_stock_full_year, stock, start_date, end_date, nifty_close): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    all_signals.extend(result)
        time.sleep(1)

    # SHEET ME DAALO
    signals_df = pd.DataFrame(all_signals)
    ws_bt_signals.clear()

    if signals_df.empty:
        ws_bt_signals.update('A1', [['Date', 'Stock', 'Entry', 'SL', 'Target', 'Score', 'Reason'],
                                    ['No Ghost Jan-Jun 2026', '', '', '', '', '', 'BUG HAI']])
        print(f"\n=== RESULT: 0 Ghost setups Jan-Jun 2026 ===", flush=True)
        print(f"=== MATLAB YA TO LOGIC BAKWAS HAI YA 9 JUNE WALA STOCK LIST ME NAHI THA ===", flush=True)
    else:
        ws_bt_signals.update('A1', [signals_df.columns.tolist()] + signals_df.values.tolist())
        print(f"\n=== RESULT: {len(signals_df)} Ghost setups found ===", flush=True)
        print(f"Stocks: {signals_df['Stock'].nunique()} unique", flush=True)
        print(f"Check Sheet: GHOST_JAN_JUNE_2026", flush=True)

if __name__ == "__main__":
    main()
