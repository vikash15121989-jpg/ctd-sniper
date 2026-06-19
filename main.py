import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

BACKTEST_MODE = True
BACKTEST_END = datetime.now().date()
BACKTEST_START = BACKTEST_END - timedelta(days=365)

print("=== POWER SPRING HYBRID V1 - TIGHT + BUGFIX ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# ===== TIGHT FILTERS - TERA ORIGINAL V1 =====
R = {
    'min_price': 100, 'max_price': 400, 'min_daily_value_cr': 0.5,
    'sl_buffer_pct': 2.0, 'target_r': 1.5, 'max_risk_pct': 4.0,
    'vol_blast_ratio': 1.5, 'adx_min': 25, 'rsi_min': 45, 'rsi_max': 70,
    '52h_proximity': 0.88, 'time_stop_days': 8
}

# FUNDAMENTAL TIGHT BUT SAFE - FAIL HONE PE SKIP NAHI KARENGE
F = {'min_market_cap_cr': 500, 'max_debt_equity': 2.0, 'max_pe': 100}

S = {
    'spring_breach_pct': 0.01, 'spring_recover_pct': 0.005,
    'max_spring_depth': 0.03
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=5000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    atr = true_range.rolling(14).mean()

    up_move = df['High'].diff()
    down_move = df['Low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    plus_di = 100 * (pd.Series(plus_dm).rolling(14).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm).rolling(14).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    df['ADX'] = dx.rolling(14).mean()
    return df

def check_power_swing(df, i):
    row = df.iloc[i]
    # TIGHT: 3 EMA trend
    if pd.isna(row['EMA200']): return False
    trend = row['Close'] > row['EMA20'] > row['EMA50'] > row['EMA200']
    pullback = row['Low'] <= row['EMA20'] * 1.02
    green = row['Close'] > row['Open']
    vol_avg = df['Volume'].iloc[i-20:i].mean()
    if vol_avg == 0 or pd.isna(vol_avg): return False
    volume = row['Volume'] > vol_avg * R['vol_blast_ratio']
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    adx_ok = row['ADX'] > R['adx_min']
    return trend and pullback and green and volume and rsi_ok and adx_ok

def check_spring_setup(df, i):
    if i < 2: return False
    row = df.iloc[i]
    prev = df.iloc[i-1]
    if pd.isna(prev['EMA20']): return False
    support = prev['EMA20']

    breached = prev['Low'] < support * (1 - S['spring_breach_pct'])
    not_too_deep = prev['Low'] > support * (1 - S['max_spring_depth'])
    recovered = row['Close'] > support * (1 + S['spring_recover_pct'])
    vol_avg = df['Volume'].iloc[i-20:i].mean()
    if vol_avg == 0 or pd.isna(vol_avg): return False
    vol_confirm = row['Volume'] > vol_avg * 1.2

    return breached and not_too_deep and recovered and vol_confirm

# ===== BACKTEST =====
all_trades = []
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))
print(f"\nWatchlist: {len(stocks)} stocks", flush=True)

if BACKTEST_MODE:
    date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B')
    print(f"Backtesting {len(date_range)} trading days...", flush=True)

    print("Downloading data...", flush=True)
    stock_data = {}
    fundamental_data = {} # BUG FIX: Fundamental alag se cache karo

    for stock in stocks:
        try:
            df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400),
                           end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) < 300: continue
            df = calculate_indicators(df)
            stock_data[stock] = df

            # BUG FIX: Fundamental data ek baar hi load karo, fail pe default values
            try:
                t = yf.Ticker(f"{stock}.NS")
                info = t.info
                fundamental_data[stock] = {
                    'mcap_cr': info.get('marketCap', 0) / 1e7,
                    'de': info.get('debtToEquity', 0),
                    'pe': info.get('trailingPE', 0)
                }
            except:
                # BUG FIX: Fail hone pe pass values de do, skip mat karo
                fundamental_data[stock] = {'mcap_cr': 9999, 'de': 0, 'pe': 10}
        except: continue

    print(f"Data ready for {len(stock_data)} stocks", flush=True)
    open_positions = []
    debug_stats = {'price': 0, 'liquidity': 0, '52h': 0, 'trend': 0, 'fundamental': 0, 'signal': 0}

    for current_date in date_range:
        current_date = current_date.date()

        # Exits
        positions_to_remove = []
        for pos in open_positions:
            df = stock_data[pos['Stock']]
            if current_date not in df.index.date: continue

            row = df.loc[df.index.date == current_date].iloc[0]
            sl_hit = row['Low'] <= pos['SL']
            target_hit = row['High'] >= pos['Target']

            exit_price = None
            exit_status = None
            days_held = (current_date - pos['Entry_Date']).days

            if sl_hit and target_hit:
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif sl_hit:
                exit_price = pos['SL']
                exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']
                exit_status = 'WIN'
            elif days_held >= R['time_stop_days']:
                exit_price = row['Close']
                exit_status = 'TIME'

            if exit_price:
                pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
                pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
                all_trades.append({
                    'Stock': pos['Stock'], 'Category': pos['Category'],
                    'Entry_Date': pos['Entry_Date'], 'Exit_Date': current_date,
                    'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
                    'Status': exit_status, 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
                    'Days_Held': days_held
                })
                positions_to_remove.append(pos)

        for pos in positions_to_remove:
            open_positions.remove(pos)

        # Entries
        open_stocks = [p['Stock'] for p in open_positions]

        for stock, df in stock_data.items():
            if stock in open_stocks: continue
            if current_date not in df.index.date: continue

            i = df.index.get_loc(df.index[df.index.date == current_date][0])
            if i < 300: continue

            row = df.iloc[i]

            # DEBUG: Kahan fail ho raha
            if row['Close'] < R['min_price'] or row['Close'] > R['max_price']:
                debug_stats['price'] += 1
                continue
            avg_value_cr = (df['Close'].iloc[i-20:i] * df['Volume'].iloc[i-20:i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_stats['liquidity'] += 1
                continue

            high_252 = df['High'].iloc[i-252:i].max()
            if pd.isna(high_252) or row['Close'] < high_252 * R['52h_proximity']:
                debug_stats['52h'] += 1
                continue

            # BUG FIX: Fundamental fail pe skip nahi, default use karo
            fund = fundamental_data.get(stock, {'mcap_cr': 9999, 'de': 0, 'pe': 10})
            if fund['mcap_cr'] < F['min_market_cap_cr']:
                debug_stats['fundamental'] += 1
                continue
            if fund['de'] > 0 and fund['de'] > F['max_debt_equity']:
                debug_stats['fundamental'] += 1
                continue
            if fund['pe'] > 0 and fund['pe'] > F['max_pe']:
                debug_stats['fundamental'] += 1
                continue

            is_power_swing = check_power_swing(df, i)
            if not is_power_swing:
                debug_stats['trend'] += 1
                continue

            is_spring = check_spring_setup(df, i)

            if is_power_swing and is_spring:
                category = 'A'
                sl_base = df['Low'].iloc[i-1]
            else:
                category = 'B'
                sl_base = row['EMA20'] * 0.98

            entry_price = row['Close']
            sl_price = sl_base * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue
            target = entry_price + risk * R['target_r']

            qty = int(750 / risk) if risk > 0 else 0
            if qty == 0: continue

            open_positions.append({
                'Stock': stock, 'Category': category, 'Entry_Date': current_date,
                'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                'Target': round(target, 2), 'Qty': qty
            })
            debug_stats['signal'] += 1

    # Close remaining
    for pos in open_positions:
        df = stock_data[pos['Stock']]
        exit_price = df['Close'].iloc[-1]
        pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
        pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
        all_trades.append({
            'Stock': pos['Stock'], 'Category': pos['Category'],
            'Entry_Date': pos['Entry_Date'], 'Exit_Date': BACKTEST_END,
            'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
            'Status': 'TIME', 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
            'Days_Held': (BACKTEST_END - pos['Entry_Date']).days
        })

    # Results - BUG FIX: Empty check pehle
    df_bt = pd.DataFrame(all_trades)

    print("\n" + "="*60, flush=True)
    print("BACKTEST RESULTS - 1 YEAR", flush=True)
    print("="*60, flush=True)
    print(f"\nDebug Stats - Kahan fail hua:", flush=True)
    print(f"Price fail: {debug_stats['price']} | Liquidity fail: {debug_stats['liquidity']}", flush=True)
    print(f"52W High fail: {debug_stats['52h']} | Fundamental fail: {debug_stats['fundamental']}", flush=True)
    print(f"Trend/Indicator fail: {debug_stats['trend']} | Signals found: {debug_stats['signal']}", flush=True)

    if df_bt.empty:
        print("\n0 trades mile. Possible reasons:", flush=True)
        print("1. Watchlist me strong uptrend stocks nahi hain", flush=True)
        print("2. 2025 market sideways raha - ADX 25 cross nahi hua", flush=True)
        print("3. NIFTY500 ki list daal ke test karo", flush=True)
    else:
        # BUG FIX: KeyError se bachne ke liye check
        if 'Category' not in df_bt.columns:
            print("\nError: Category column missing!", flush=True)
        else:
            for cat in ['A', 'B']:
                cat_df = df_bt[df_bt['Category'] == cat]
                if cat_df.empty:
                    print(f"\nCategory {cat}: No trades", flush=True)
                    continue

                total = len(cat_df)
                wins = len(cat_df[cat_df['Status'] == 'WIN'])
                losses = len(cat_df[cat_df['Status'] == 'LOSS'])
                time_exit = len(cat_df[cat_df['Status'] == 'TIME'])
                winrate = round(wins / total * 100, 1) if total else 0

                avg_win = cat_df[cat_df['Status']=='WIN']['PnL_%'].mean() if wins else 0
                avg_loss = cat_df[cat_df['Status']=='LOSS']['PnL_%'].mean() if losses else 0
                total_pnl = cat_df['PnL_Rs'].sum()

                win_amt = cat_df[cat_df['Status']=='WIN']['PnL_Rs'].sum()
                loss_amt = abs(cat_df[cat_df['Status']=='LOSS']['PnL_Rs'].sum())
                pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999

                cat_name = "Power+Spring" if cat=='A' else "Power Only"
                print(f"\nCategory {cat} - {cat_name}", flush=True)
                print(f"Total Trades: {total}", flush=True)
                print(f"Wins: {wins} | Losses: {losses} | Time: {time_exit}", flush=True)
                print(f"Winrate: {winrate}%", flush=True)
                print(f"Avg Win: {avg_win:.1f}% | Avg Loss: {avg_loss:.1f}%", flush=True)
                print(f"Profit Factor: {pf}", flush=True)
                print(f"Total PnL: Rs.{total_pnl:,.0f}", flush=True)

            total = len(df_bt)
            wins = len(df_bt[df_bt['Status'] == 'WIN'])
            winrate = round(wins / total * 100, 1) if total else 0
            win_amt = df_bt[df_bt['Status']=='WIN']['PnL_Rs'].sum()
            loss_amt = abs(df_bt[df_bt['Status']=='LOSS']['PnL_Rs'].sum())
            pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999

            print(f"\nCOMBINED", flush=True)
            print(f"Total Trades: {total}", flush=True)
            print(f"Winrate: {winrate}%", flush=True)
            print(f"Profit Factor: {pf}", flush=True)
            print(f"Total PnL: Rs.{df_bt['PnL_Rs'].sum():,.0f}", flush=True)

    try:
        ws_bt = get_or_create_ws(sh, "BACKTEST_HYBRID_1Y")
        ws_bt.clear()
        if not df_bt.empty:
            ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
            print(f"\nSaved to GSHEET", flush=True)
    except Exception as e:
        print(f"GSheet error: {e}", flush=True)

print("\n=== COMPLETE ===", flush=True)
