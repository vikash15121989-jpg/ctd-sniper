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

print("=== V22.0 GHOST: 20 EMA SQUEEZE EDITION ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG - 20 EMA SQUEEZE =====
R = {
    'backtest_start': '2023-01-01', # 2026 dead hai. 2023 pe test kar
    'backtest_end': '2023-12-31',
    'batch_size': 50,
    'max_workers': 10,

    # FILTER
    'min_price': 30,
    'min_avg_volume': 30000,
    'min_daily_turnover': 1e6,

    # 20 EMA SQUEEZE LOGIC
    'ema_period': 20,
    'ema_zone_pct': 0.03, # 20 EMA se ±3% me ho to bhi chalega
    'lookback': 10, # 10 days squeeze
    'hold_days': 10, # Entry ke baad 10 din ka move
    'choch_lookback': 20, # CHoCH ke liye 20 din
}

def get_or_create_ws(sh, title, rows=5000, cols=20):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = get_or_create_ws(sh, "Watchlist")
ws_bt_signals = get_or_create_ws(sh, "GHOST_20EMA_SQUEEZE_2023")

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
        df = yf.download(ticker, start=start_date - timedelta(days=400), end=end_date + timedelta(days=1),
                         progress=False, auto_adjust=False, group_by='column', timeout=15)
        if df.empty or len(df) < 100: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
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

def is_choch_uptrend(df, lookback=20):
    """Simple CHoCH: Recent 20D me Higher High + Higher Low"""
    if len(df) < lookback: return False
    recent = df.tail(lookback)
    highs = recent['High'].rolling(5).max().dropna()
    lows = recent['Low'].rolling(5).min().dropna()
    if len(highs) < 2 or len(lows) < 2: return False
    hh = highs.iloc[-1] > highs.iloc[-2]
    hl = lows.iloc[-1] > lows.iloc[-2]
    return hh and hl

def backtest_20ema_squeeze(stock, start_date, end_date):
    try:
        df = download_stock_data(stock, start_date, end_date)
        if df is None or len(df) < 100: return []

        avg_price = df['Close'].tail(20).mean()
        avg_vol = df['Volume'].tail(20).mean()
        avg_turnover = (df['Close'] * df['Volume']).tail(20).mean()

        if avg_price < R['min_price']: return []
        if avg_vol < R['min_avg_volume']: return []
        if avg_turnover < R['min_daily_turnover']: return []

        # INDICATORS
        df['EMA20'] = df['Close'].ewm(span=R['ema_period'], adjust=False).mean()
        df['10D_Max_High'] = df['High'].rolling(R['lookback']).max().shift(1)
        df['10D_Max_Vol'] = df['Volume'].rolling(R['lookback']).max().shift(1)

        trades = []
        df_scan = df[(df.index >= start_date) & (df.index <= end_date)]
        if df_scan.empty: return []
        start_idx = df.index.get_loc(df_scan.index[0])

        for i in range(start_idx, len(df) - R['hold_days']):
            row = df.iloc[i]

            # 1. 20 EMA FILTER: Upar ho ya paas me ho
            ema = row['EMA20']
            close = row['Close']
            in_ema_zone = close >= ema * (1 - R['ema_zone_pct']) # 3% neeche tak chalega
            if not in_ema_zone:
                continue

            # 2. CHoCH FILTER
            if not is_choch_uptrend(df.iloc[:i+1], R['choch_lookback']):
                continue

            # 3. SQUEEZE CONDITION
            vol_condition = row['Volume'] > df.iloc[i]['10D_Max_Vol']
            price_condition = row['High'] < df.iloc[i]['10D_Max_High']

            if vol_condition and price_condition:
                resistance = df.iloc[i]['10D_Max_High']
                signal_date = df.index[i]

                # 4. AGLE 10 DIN ME BREAKOUT DHUNDO
                future = df.iloc[i+1 : i+1+R['hold_days']]
                breakout = future[future['High'] >= resistance]

                if not breakout.empty:
                    entry_date = breakout.index[0]
                    entry_price = resistance # Conservative entry

                    # 5. ENTRY KE BAAD MAX UP/DOWN
                    post_entry = df.loc[entry_date : entry_date + timedelta(days=R['hold_days'])]
                    if len(post_entry) == 0: continue

                    max_up = round((post_entry['High'].max() / entry_price - 1) * 100, 2)
                    max_down = round((post_entry['Low'].min() / entry_price - 1) * 100, 2)
                    days_to_bo = (entry_date - signal_date).days

                    trades.append({
                        'Signal_Date': signal_date.date().strftime('%Y-%m-%d'),
                        'Stock': stock.replace('.NS', ''),
                        'EMA20': round(ema, 2),
                        'Close': round(close, 2),
                        'Resistance': round(resistance, 2),
                        'Entry_Date': entry_date.date().strftime('%Y-%m-%d'),
                        'Entry_Price': round(entry_price, 2),
                        'Max_Up_%': max_up,
                        'Max_Down_%': max_down,
                        'Days_to_BO': days_to_bo
                    })
                    print(f"SQUEEZE: {stock.replace('.NS','')} {signal_date.date()} EMA:{ema:.0f} Resist:{resistance:.0f} BO:{days_to_bo}D Up:{max_up}%", flush=True)

        return trades
    except Exception as e:
        return []

def main():
    start_date = pd.to_datetime(R['backtest_start']).date()
    end_date = pd.to_datetime(R['backtest_end']).date()
    print(f"\n=== SCANNING {start_date} to {end_date} - 20 EMA SQUEEZE ===", flush=True)
    print(f"Rules: 20EMA Zone + CHoCH + Vol>10D Max + High<10D Max", flush=True)

    stock_universe = get_watchlist_stocks()
    all_trades = []

    for i in range(0, len(stock_universe), R['batch_size']):
        batch = stock_universe[i:i+R['batch_size']]
        print(f"\nBatch {i//R['batch_size']+1}: Scanning {len(batch)} stocks...", flush=True)

        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(backtest_20ema_squeeze, stock, start_date, end_date): stock for stock in batch}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    all_trades.extend(result)
        time.sleep(1)

    results_df = pd.DataFrame(all_trades)
    ws_bt_signals.clear()

    if results_df.empty:
        ws_bt_signals.update('A1', [['No Squeeze Found in this period', '', '', '', '', '']])
        print(f"\n=== RESULT: 0 Signal - Market me setup hi nahi bana ===", flush=True)
    else:
        results_df = results_df.sort_values('Max_Up_%', ascending=False)
        ws_bt_signals.update('A1', [results_df.columns.tolist()] + results_df.values.tolist())
        print(f"\n=== RESULT: {len(results_df)} Squeeze setups ===", flush=True)
        print(f"Avg Max Up: {results_df['Max_Up_%'].mean():.2f}%")
        print(f"Avg Max Down: {results_df['Max_Down_%'].mean():.2f}%")
        print(f"Win Rate >5%: {(results_df['Max_Up_%'] > 5).sum() / len(results_df) * 100:.1f}%")
        print(f"Avg Days to Breakout: {results_df['Days_to_BO'].mean():.1f} days")

if __name__ == "__main__":
    main()
