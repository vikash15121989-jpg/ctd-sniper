import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time  # <-- FIX 1: time module import kiya
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== V21: Watchlist + 10D PA + 2Y Backtest + Tradable Filter ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. CONFIG =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

R = {
    'lookback_days': 10,
    'min_price': 60,
    'min_avg_volume': 500000,
    'min_daily_turnover': 3e7,
    'swing_window': 5,
    'base_range_max': 7.0,
    'volume_multiplier': 1.8,
    'candle_close_pos': 0.7,
    'candle_body_pct': 0.6,
    'min_winrate': 50.0,
    'min_trades_bt': 5,
    'rr_ratio': 2.0,
    'max_hold_days': 15,
    'sl_loss_pct': 0.05,
    'target_pct': 0.12
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

# ===== 2. WATCHLIST SE STOCK UTHAO =====
def get_watchlist_stocks():
    try:
        stocks = ws_watchlist.col_values(1)
        stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
        stocks = [s + '.NS' if not s.endswith('.NS') else s for s in stocks]

        if not stocks:
            print("Watchlist khali hai. Default 10 stocks le raha hu", flush=True)
            return ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
                    "SBIN.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "KOTAKBANK.NS", "LT.NS"]

        print(f"Watchlist se {len(stocks)} stocks mile", flush=True)
        return stocks
    except Exception as e:
        print(f"Watchlist error: {e}. Default list use kar raha hu", flush=True)
        return ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS"]

# ===== 3. PRICE ACTION CORE =====
def download_stock_data(ticker, start_date, end_date):
    try:
        df = yf.download(ticker, start=start_date, end=end_date + timedelta(days=1),
                         progress=False, auto_adjust=False, timeout=10)
        if df.empty or len(df) < 60: return None
        
        # FIX 3: MultiIndex and Column Flattening safety
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # Ensure 1D structure for columns
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
    # shift(-w) की वजह से आखिरी w कैंडल का स्विंग हाई कभी कैलकुलेट नहीं होगा (यह नॉर्मल बिहेवियर है)
    df['Swing_High'] = df['High'][(df['High'].shift(w) < df['High']) &
                                  (df['High'].shift(-w) < df['High'])]
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    return df

def is_pure_price_action_signal(df, idx):
    if idx < 50: return False, "Not enough data", None
    row = df.iloc[idx]
    prev_row = df.iloc[idx-1]

    # RULE 1: MARKET STRUCTURE - Higher High
    # स्विंग हाई देखने के लिए हमें ब्रेकआउट वाले दिन (idx) से पहले का डेटा देखना चाहिए
    historical_swings = df['Swing_High'].iloc[:idx].dropna().tail(3)
    if len(historical_swings) < 2: return False, "No HH", None
    if historical_swings.iloc[-1] <= historical_swings.iloc[-2]: return False, "Not HH", None

    # RULE 2: CONSOLIDATION - 10 din tight base
    base_df = df.iloc[idx-10:idx]
    base_high = base_df['High'].max()
    base_low = base_df['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100
    if base_range_pct > R['base_range_max']: return False, f"Range {base_range_pct:.1f}%", None

    # RULE 3: BREAKOUT QUALITY
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

# ===== 4. PHASE 1: 10D CANDIDATES =====
def scan_10d_pa_candidates(universe, end_date):
    candidates = []
    start_date = end_date - timedelta(days=R['lookback_days'] + 100)

    for i, stock in enumerate(universe):
        print(f"Scanning {i+1}/{len(universe)}: {stock}", flush=True)
        df = download_stock_data(stock, start_date, end_date)
        if df is None: continue

        avg_vol = df['Volume'].tail(20).mean()
        avg_turnover = (df['Close'] * df['Volume']).tail(20).mean()
        if avg_vol < R['min_avg_volume'] or avg_turnover < R['min_daily_turnover']: continue
        if df['Close'].iloc[-1] < R['min_price']: continue

        df = find_swing_points(df)
        scan_start = max(50, len(df) - R['lookback_days'])

        for idx in range(scan_start, len(df)):
            is_signal, reason, signal_data = is_pure_price_action_signal(df, idx)
            if is_signal:
                candidates.append({
                    'Stock': stock.replace('.NS', ''),
                    'Signal_Date': df.index[idx].date().strftime('%Y-%m-%d'),
                    'Close': signal_data['Entry'],
                    'SL': signal_data['SL'],          # <-- FIX 2: SL/Target यहीं सेव कर लिया
                    'Target': signal_data['Target'],  # ताकि Phase 3 में री-कैलकुलेट न करना पड़े
                    'Volume': int(df['Volume'].iloc[idx]),
                    'Reason': reason
                })
                break
        time.sleep(0.1)

    return pd.DataFrame(candidates)

# ===== 5. PHASE 2: 2 YEAR BACKTEST =====
def backtest_pa_stock(stock, end_date):
    df = download_stock_data(f"{stock}.NS", end_date - timedelta(days=730), end_date)
    if df is None: return None

    df = find_swing_points(df)
    trades = []
    in_trade = False
    entry = sl = tp = 0
    entry_idx = 0

    # Backtest loop avoids running into last few days improperly
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

    if len(trades) < R['min_trades_bt']: return None

    wins = sum(1 for t in trades if t > 0)
    winrate = wins / len(trades) * 100
    pf = wins / (len(trades) - wins) if len(trades) - wins > 0 else 99

    return {
        'Stock': stock,
        'Total_Trades': len(trades),
        'Wins': wins,
        'Losses': len(trades) - wins,
        'WinRate': round(winrate, 2),
        'Profit_Factor': round(pf, 2)
    }

# ===== 6. MAIN V21 =====
def main():
    today = datetime.now().date()
    print(f"\n=== V21 PA Scanner {today} ===", flush=True)

    stock_universe = get_watchlist_stocks()

    # PHASE 1
    print("\nPHASE 1: Scanning last 10 days PA...", flush=True)
    candidates_df = scan_10d_pa_candidates(stock_universe, today)
    if candidates_df.empty:
        print("No PA candidates found in last 10 days")
        ws_candidates.clear()
        ws_candidates.update('A1', [['Stock', 'Signal_Date', 'Close', 'Reason'], ['No signals', '', '', '']])
        ws_backtest.clear()
        ws_tradable.clear()
        return
    
    # Save clean copy for Sheet
    sheet_candidates = candidates_df[['Stock', 'Signal_Date', 'Close', 'Volume', 'Reason']]
    ws_candidates.clear()
    ws_candidates.update('A1', [sheet_candidates.columns.tolist()] + sheet_candidates.values.tolist())

    # PHASE 2
    print(f"\nPHASE 2: Backtesting {len(candidates_df)} candidates for 2 years...", flush=True)
    backtest_results = []
    for idx, stock in enumerate(candidates_df['Stock'].unique()):
        print(f"Backtesting {idx+1}/{len(candidates_df['Stock'].unique())}: {stock}", flush=True)
        result = backtest_pa_stock(stock, today)
        if result: backtest_results.append(result)
        time.sleep(0.2)

    if not backtest_results:
        print("No backtest results")
        ws_backtest.clear()
        ws_tradable.clear()
        return

    backtest_df = pd.DataFrame(backtest_results)
    ws_backtest.clear()
    ws_backtest.update('A1', [backtest_df.columns.tolist()] + backtest_df.values.tolist())

    # PHASE 3: FIX 2 - Re-scanning hata kar seedhe candidates_df se merge kiya
    tradable = backtest_df[backtest_df['WinRate'] >= R['min_winrate']].copy()
    print(f"\nPHASE 3: {len(tradable)} stocks with WR >= {R['min_winrate']}%", flush=True)

    tradable_final = []
    for _, row in tradable.iterrows():
        stock_name = row['Stock']
        # Find entry/SL details from Phase 1 data directly
        match = candidates_df[candidates_df['Stock'] == stock_name].iloc[-1]
        
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

    print(f"\n=== V21 COMPLETE: {len(tradable_df)} Tradable Stocks ===", flush=True)

if __name__ == "__main__":
    main()
    
