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

print("=== V23.0 GHOST: WATCHLIST CHoCH SQUEEZE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
R = {
    'backtest_start': '2025-01-01',
    'backtest_end': '2026-06-24',
    'batch_size': 20, # Rate limit se bachne ke liye
    'ema_zone_pct': 0.03, # 20 EMA se ±3% me
    'lookback_days': 10, # 10D High/Vol ke liye
    'hold_days': 10, # Entry ke baad kitna move dekhe
    'choch_window': 50, # CHoCH check karne ke liye 50D
}

def get_or_create_ws(sh, title, rows=5000, cols=20):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

# ===== GSHEET SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = get_or_create_ws(sh, "Watchlist")
ws_output = get_or_create_ws(sh, "CHoCH_SQUEEZE_SIGNALS")

def get_watchlist_stocks():
    """Watchlist sheet se stock uthao"""
    try:
        stocks = ws_watchlist.col_values(1)
        # Header aur khali hatao
        stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME', 'TICKER']]
        #.NS lagao agar nahi hai
        stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
        print(f"Watchlist Loaded: {len(stocks)} stocks", flush=True)
        if stocks:
            print(f"First 5: {stocks[:5]}", flush=True)
        return stocks if stocks else ["RELIANCE.NS", "TCS.NS"]
    except Exception as e:
        print(f"Watchlist error: {e}", flush=True)
        return ["RELIANCE.NS", "TCS.NS"]

def detect_choch_and_trend(df, idx):
    """
    Check: 1. CHoCH hua hai 2. Ab HH-HL me hai
    """
    if idx < R['choch_window']: return False

    # PART 1: CHoCH - Downtrend se Uptrend flip
    window = df.iloc[idx-R['choch_window']:idx+1]
    mid = len(window) // 2

    # Pehle half: Downtrend tha?
    first_lows = window['Low'].iloc[:mid]
    first_highs = window['High'].iloc[:mid]
    downtrend = first_lows.iloc[-1] < first_lows.iloc[0] and first_highs.iloc[-1] < first_highs.iloc[0]

    # Second half: Uptrend bana?
    second_lows = window['Low'].iloc[mid:]
    second_highs = window['High'].iloc[mid:]
    uptrend = second_lows.iloc[-1] > second_lows.iloc[0] and second_highs.iloc[-1] > second_highs.iloc[0]

    choch_done = downtrend and uptrend
    if not choch_done: return False

    # PART 2: Abhi HH-HL hai? Last 10D
    last_10 = df.iloc[idx-9:idx+1]
    hh = last_10['High'].iloc[-1] > last_10['High'].iloc[0]
    hl = last_10['Low'].iloc[-1] > last_10['Low'].iloc[0]

    return hh and hl

def scan_stock(stock, start_date, end_date):
    try:
        time.sleep(1.2) # Rate limit: 1.2 sec per stock
        df = yf.download(stock, start=start_date - timedelta(days=200), end=end_date + timedelta(days=1), progress=False, timeout=20)
        if df.empty or len(df) < 100:
            return []

        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['10D_High'] = df['High'].rolling(R['lookback_days']).max().shift(1)
        df['10D_Vol'] = df['Volume'].rolling(R['lookback_days']).max().shift(1)

        df_scan = df[(df.index >= start_date) & (df.index <= end_date)]
        signals = []

        for i in range(len(df_scan)):
            idx = df.index.get_loc(df_scan.index[i])
            if idx < 50: continue
            row = df.iloc[idx]

            # FILTER 1: 20 EMA ke paas
            if row['Close'] < row['EMA20'] * (1 - R['ema_zone_pct']):
                continue

            # FILTER 2: CHoCH + HH-HL
            if not detect_choch_and_trend(df, idx):
                continue

            # FILTER 3: SQUEEZE - Last 3 din me volume blast + Aaj high daba
            vol_blast = False
            for j in range(max(10, idx-2), idx+1):
                if df.iloc[j]['Volume'] > df.iloc[j]['10D_Vol']:
                    vol_blast = True
                    break
            if not vol_blast: continue
            if row['High'] >= row['10D_High']: continue

            # SIGNAL MILA - Breakout check
            resistance = row['10D_High']
            signal_date = df.index[idx].date()

            future = df.iloc[idx+1:idx+1+R['hold_days']]
            breakout = future[future['High'] >= resistance]

            if not breakout.empty:
                entry_date = breakout.index[0]
                entry_price = resistance
                post_entry = df.loc[entry_date:entry_date + timedelta(days=R['hold_days'])]

                max_up = round((post_entry['High'].max() / entry_price - 1) * 100, 2)
                max_down = round((post_entry['Low'].min() / entry_price - 1) * 100, 2)
                days_to_bo = (entry_date.date() - signal_date).days

                signals.append({
                    'Signal_Date': str(signal_date),
                    'Stock': stock.replace('.NS', ''),
                    'EMA20': round(row['EMA20'], 2),
                    'Close': round(row['Close'], 2),
                    'Resistance': round(resistance, 2),
                    'Entry_Date': str(entry_date.date()),
                    'Entry_Price': round(entry_price, 2),
                    'Max_Up_%': max_up,
                    'Max_Down_%': max_down,
                    'Days_to_BO': days_to_bo
                })
                print(f"SIGNAL: {stock.replace('.NS','')} {signal_date} Entry:{entry_price:.0f} Up:{max_up}% Down:{max_down}%", flush=True)

        return signals
    except Exception as e:
        print(f"{stock}: Error - {e}", flush=True)
        return []

def main():
    start_date = pd.to_datetime(R['backtest_start']).date()
    end_date = pd.to_datetime(R['backtest_end']).date()
    print(f"\n=== SCANNING {start_date} to {end_date} ===", flush=True)
    print(f"Logic: CHoCH + HH-HL + 20EMA + Volume Squeeze", flush=True)

    stock_universe = get_watchlist_stocks()
    all_signals = []

    # Batch me chala - Rate limit se bachne ke liye
    for i in range(0, len(stock_universe), R['batch_size']):
        batch = stock_universe[i:i+R['batch_size']]
        print(f"\nBatch {i//R['batch_size']+1}/{(len(stock_universe)-1)//R['batch_size']+1}: {len(batch)} stocks...", flush=True)

        for stock in batch:
            result = scan_stock(stock, start_date, end_date)
            if result:
                all_signals.extend(result)

        time.sleep(5) # Batch ke baad 5 sec break

    # SHEET ME LIKHO
    ws_output.clear()
    if all_signals:
        df_final = pd.DataFrame(all_signals)
        df_final = df_final.sort_values('Max_Up_%', ascending=False)
        ws_output.update('A1', [df_final.columns.tolist()] + df_final.values.tolist())
        print(f"\n=== TOTAL SIGNALS: {len(df_final)} ===", flush=True)
        print(f"Avg Max Up: {df_final['Max_Up_%'].mean():.2f}%", flush=True)
        print(f"Win Rate >5%: {(df_final['Max_Up_%'] > 5).sum() / len(df_final) * 100:.1f}%", flush=True)
        print(f"Sheet Updated: CHoCH_SQUEEZE_SIGNALS", flush=True)
    else:
        ws_output.update('A1', [['No Signals Found', f'{start_date} to {end_date}']])
        print(f"\n=== 0 SIGNAL - Is period me CHoCH + Squeeze nahi bana ===", flush=True)

if __name__ == "__main__":
    main()
