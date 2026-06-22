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

print("=== V17.8: DUPLICATE ENTRY KILLER ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. CONFIG =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_price': 60,
    'max_hold_days': 30,
    'target_pct': 0.12,
    'sl_loss_pct': 0.05,
    'lookback_trading_days': 10,
    'min_wr_for_vip': 0.50,
    'vip_min_trades': 4,
    'min_avg_volume': 500000,
    'min_daily_turnover': 30000000,
    'max_workers': 15
}

def get_or_create_ws(sh, title, rows=1000, cols=10):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
ws_active = get_or_create_ws(sh, "ACTIVE_TRADES")

def safe_float(val, default=0.0):
    try:
        if val == '' or val is None: return default
        return float(str(val).replace(',', '').strip())
    except:
        return default

# ===== 2. INDICATORS =====
def build_indicators(df):
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) < 35: return df

    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_20MA'] = df['Turnover'].shift(1).rolling(window=20).mean()
    return df

def check_liquidity(df, idx):
    if idx < 20 or len(df) <= idx: return False
    row = df.iloc[idx]
    avg_vol, avg_turnover = row['Vol_20MA'], row['Turnover_20MA']
    if pd.isna(avg_vol) or pd.isna(avg_turnover): return False
    return avg_vol >= R['min_avg_volume'] and avg_turnover >= R['min_daily_turnover']

def check_breakout_signal(df, idx):
    if idx < 1: return False
    row, row_prev = df.iloc[idx], df.iloc[idx-1]
    if pd.isna(row['EMA_50']) or pd.isna(row['Breakout_High_20D']) or pd.isna(row['Vol_Multiple']): return False
    if not check_liquidity(df, idx): return False
    if row['Close'] < row['EMA_50']: return False
    breakout_level = row['Breakout_High_20D']
    fresh_breakout = (row['Close'] > breakout_level) and (row_prev['Close'] <= breakout_level or row['Open'] > breakout_level)
    good_volume = row['Vol_Multiple'] > 1.2
    is_green = row['Close'] > row['Open']
    return fresh_breakout and good_volume and is_green

# ===== 3. MAIN =====
def main():
    today = datetime.now().date()

    # PHASE 1: VIP LOAD - same as before
    vip_stocks = []
    try:
        all_vip_data = ws_filter.get_all_values()
        if len(all_vip_data) > 1 and all_vip_data[0][0].startswith("LOCK_UNTIL:"):
            lock_date = datetime.strptime(all_vip_data[0][0].split(":")[1].strip(), '%Y-%m-%d').date()
            if today <= lock_date:
                vip_stocks = [row[0] for row in all_vip_data[2:] if row[0]]
    except: pass

    print(f"🎯 VIP Stocks: {len(vip_stocks)}", flush=True)

    # PHASE 2: LOAD + CLEAN DUPLICATES FROM SHEET
    print("\n[PHASE 2] Loading + Cleaning Duplicates...", flush=True)
    try:
        master_rows = ws_active.get_all_records()
    except:
        master_rows = []

    # 🎯 LOCK 1: Sheet se duplicate OPEN hatao
    seen_open = set()
    all_historical_trades = []
    dup_count = 0
    for trade in master_rows:
        if trade.get('Status') == 'OPEN':
            key = trade.get('Stock_Name')
            if key in seen_open:
                dup_count += 1
                continue
            seen_open.add(key)
        all_historical_trades.append(trade)

    if dup_count > 0:
        print(f"🧹 Removed {dup_count} duplicate OPEN trades from sheet", flush=True)

    # Update status of existing trades
    open_stocks_before = {r['Stock_Name'] for r in all_historical_trades if r.get('Status') == 'OPEN'}

    for stock in list(open_stocks_before):
        trade = next((t for t in all_historical_trades if t.get('Stock_Name') == stock and t.get('Status') == 'OPEN'), None)
        if not trade: continue
        try:
            sig_date_str = trade.get('Signal_Date')
            sl_val = safe_float(trade.get('StopLoss_Price'))
            tgt_val = safe_float(trade.get('Target_Price'))
            sig_date = datetime.strptime(sig_date_str, '%Y-%m-%d').date()
            entry_start_date = sig_date + timedelta(days=1)
            if entry_start_date > today: continue

            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=False)
            if df.empty: continue
            df_after_signal = df[df.index.date >= entry_start_date]
            if df_after_signal.empty: continue

            trade_status = "OPEN"
            exit_date_str = ""
            for current_date, row in df_after_signal.iterrows():
                day_low, day_high, day_close = float(row['Low']), float(row['High']), float(row['Close'])
                date_str = current_date.date().strftime('%Y-%m-%d')
                hit_sl, hit_tgt = day_low <= sl_val, day_high >= tgt_val

                if hit_sl and hit_tgt:
                    if day_close <= sl_val:
                        trade_status = "LOSS"
                    elif day_close >= tgt_val:
                        trade_status = "PROFIT"
                    else:
                        continue
                    exit_date_str = date_str
                    break
                elif hit_sl:
                    trade_status = "LOSS"
                    exit_date_str = date_str
                    break
                elif hit_tgt:
                    trade_status = "PROFIT"
                    exit_date_str = date_str
                    break

            if trade_status == "OPEN" and (today - sig_date).days >= R['max_hold_days']:
                trade_status = "TIMEOUT"
                exit_date_str = today.strftime('%Y-%m-%d')

            if trade_status!= "OPEN":
                for idx, item in enumerate(all_historical_trades):
                    if item.get('Stock_Name') == stock and item.get('Status') == 'OPEN' and item.get('Signal_Date') == sig_date_str:
                        all_historical_trades[idx]['Status'] = trade_status
                        all_historical_trades[idx]['Exit_Date'] = exit_date_str
                        print(f"🎯 {stock} closed: {trade_status}", flush=True)
                        break
        except Exception as e:
            print(f"Error tracking {stock}: {e}", flush=True)

    # PHASE 3: NEW SIGNAL SCAN
    print(f"\n[PHASE 3] Scanning for new signals...", flush=True)
    live_signals_pool = []

    # 🎯 LOCK 2: Updated OPEN list after status update
    open_trade_stocks = {r['Stock_Name'] for r in all_historical_trades if r.get('Status') == 'OPEN'}
    print(f"🔒 Stocks with OPEN trade: {len(open_trade_stocks)}", flush=True)

    for stock in vip_stocks:
        # 🎯 MAIN FIX: Agar OPEN hai to bilkul skip
        if stock in open_trade_stocks:
            continue

        try:
            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=False)
            if df.empty or len(df) < 35: continue
            df = build_indicators(df)
            total_rows = len(df)
            last_trading_date_str = df.index[-1].strftime('%Y-%m-%d')
            start_idx = max(21, total_rows - R['lookback_trading_days'])

            for idx in range(start_idx, total_rows):
                if df.iloc[idx]['Close'] < R['min_price']: continue
                if check_breakout_signal(df, idx):
                    row_sig = df.iloc[idx]
                    sig_date_str = df.index[idx].strftime('%Y-%m-%d')

                    # Extra safety: same date check
                    if any(h.get('Stock_Name') == stock and h.get('Signal_Date') == sig_date_str for h in all_historical_trades):
                        continue

                    ep = round(float(row_sig['Close']), 2)
                    sl = round(ep * (1 - R['sl_loss_pct']), 2)
                    tgt = round(ep * (1 + R['target_pct']), 2)

                    if sig_date_str == last_trading_date_str:
                        live_signals_pool.append({
                            'Stock_Name': stock, 'Signal_Date': sig_date_str,
                            'Entry_Price': ep, 'StopLoss_Price': sl, 'Target_Price': tgt
                        })

                    all_historical_trades.append({
                        'Stock_Name': stock, 'Signal_Date': sig_date_str,
                        'Entry_Price': ep, 'Target_Price': tgt, 'StopLoss_Price': sl,
                        'Exit_Date': '', 'Status': 'OPEN'
                    })

                    # 🎯 LOCK 3: Abhi add kiya to turant OPEN list me daal de
                    open_trade_stocks.add(stock)
                    break
        except:
            continue

    # PHASE 4: SYNC
    try:
        ws_active.clear()
        master_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'Target_Price', 'StopLoss_Price', 'Exit_Date', 'Status']
        if all_historical_trades:
            df_master = pd.DataFrame(all_historical_trades)
            df_master['Signal_Date_DT'] = pd.to_datetime(df_master['Signal_Date'])
            df_master = df_master.sort_values(by='Signal_Date_DT', ascending=False).drop(columns=['Signal_Date_DT'])
            df_master = df_master.reindex(columns=master_headers).fillna("")
            ws_active.update('A1', [master_headers] + df_master.values.tolist())

        ws_signals.clear()
        signal_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']
        if live_signals_pool:
            df_sig = pd.DataFrame(live_signals_pool)
            ws_signals.update('A1', [signal_headers] + df_sig.values.tolist())
            print(f"🚀 {len(df_sig)} Fresh Signals", flush=True)
        else:
            ws_signals.update('A1', [signal_headers] + [["No new signals today", "", "", "", ""]])
    except Exception as e:
        print(f"❌ Sheet Error: {e}", flush=True)

    print(f"\n=== V17.8 COMPLETE ===", flush=True)

if __name__ == "__main__":
    main()
