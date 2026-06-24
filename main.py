import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== V18.7: MAX-BASED + NO LOCK ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. CONFIG =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

R = {
    'min_price': 60,
    'max_hold_days': 30,
    'target_pct': 0.12, # 12%
    'sl_loss_pct': 0.05, # 5%
    'lookback_trading_days': 10,
    'min_wr_for_vip': 0.50,
    'vip_min_trades': 4,
    'min_avg_volume': 500000,
    'min_daily_turnover': 30000000,
    'trailing_min_pct': 0.05, # 5%
    'trailing_max_pct': 0.12 # 12%
}

def get_or_create_ws(sh, title, rows=1000, cols=15):
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

def find_first_breakout(df, start_idx, end_idx):
    for idx in range(start_idx, end_idx):
        if idx < 1: continue
        row, row_prev = df.iloc[idx], df.iloc[idx-1]
        if pd.isna(row['EMA_50']) or pd.isna(row['Breakout_High_20D']) or pd.isna(row['Vol_Multiple']):
            continue
        if not check_liquidity(df, idx): continue
        if row['Close'] < row['EMA_50']: continue
        breakout_level = row['Breakout_High_20D']
        fresh_breakout = (row['Close'] > breakout_level) and (row_prev['Close'] <= breakout_level)
        good_volume = row['Vol_Multiple'] > 1.2
        is_green = row['Close'] > row['Open']
        if fresh_breakout and good_volume and is_green:
            return idx, breakout_level
    return None, None

# ===== 3. MAIN =====
def main():
    today = datetime.now().date()

    # PHASE 1: VIP LOAD - 🎯 LOCK HATA DIYA
    vip_stocks = []
    try:
        all_vip_data = ws_filter.get_all_values()
        if len(all_vip_data) > 1:
            start_row = 2 if all_vip_data[0][0].startswith("LOCK_UNTIL:") else 1
            vip_stocks = [row[0] for row in all_vip_data[start_row:] if row[0]]
    except Exception as e:
        print(f"VIP Load Error: {e}", flush=True)
    print(f"🎯 VIP Stocks: {len(vip_stocks)} - Fresh Load Every Run", flush=True)

    # PHASE 2: FULL REBUILD WITH MAX/MIN LOGIC
    print("\n[PHASE 2] Rebuilding with MAX-based PCT...", flush=True)
    try:
        master_rows = ws_active.get_all_records()
    except:
        master_rows = []

    # Latest signal per stock
    temp_trades = {}
    for trade in master_rows:
        stock = trade.get('Stock_Name')
        sig_date = trade.get('Signal_Date')
        if not stock or not sig_date: continue
        if stock not in temp_trades:
            temp_trades[stock] = trade
        else:
            old_date = datetime.strptime(temp_trades[stock]['Signal_Date'], '%Y-%m-%d').date()
            new_date = datetime.strptime(sig_date, '%Y-%m-%d').date()
            if new_date > old_date:
                temp_trades[stock] = trade

    all_historical_trades = []

    # CORE LOGIC: MAX/MIN BASED
    for stock, trade in temp_trades.items():
        try:
            sig_date_str = trade.get('Signal_Date')
            entry_val = safe_float(trade.get('Entry_Price'))
            sl_val = safe_float(trade.get('StopLoss_Price'))
            tgt_val = entry_val * (1 + R['target_pct'])
            sig_date = datetime.strptime(sig_date_str, '%Y-%m-%d').date()
            entry_start_date = sig_date + timedelta(days=1)

            if entry_start_date > today:
                trade['Status'] = 'OPEN'
                trade['PCT_FROM_ENTRY'] = 0.0
                all_historical_trades.append(trade)
                continue

            df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=False, end=today + timedelta(days=1))
            if df.empty:
                trade['Status'] = 'OPEN'
                trade['PCT_FROM_ENTRY'] = 0.0
                all_historical_trades.append(trade)
                continue

            df_after_signal = df[df.index.date >= entry_start_date]
            if df_after_signal.empty:
                trade['Status'] = 'OPEN'
                trade['PCT_FROM_ENTRY'] = 0.0
                all_historical_trades.append(trade)
                continue

            max_high = float(df_after_signal['High'].max())
            min_low = float(df_after_signal['Low'].min())
            max_pct_from_entry = round(((max_high - entry_val) / entry_val) * 100, 2)
            trade['PCT_FROM_ENTRY'] = max_pct_from_entry

            print(f"{stock}: Entry={entry_val}, MAX={max_high} ({max_pct_from_entry}%), MIN={min_low}", flush=True)

            # STATUS LOGIC
            if max_high >= tgt_val:
                trade['Status'] = "PROFIT"
                trade['Exit_Date'] = df_after_signal[df_after_signal['High'] >= tgt_val].index[0].date().strftime('%Y-%m-%d')

            elif min_low <= sl_val:
                trade['Status'] = "LOSS"
                trade['Exit_Date'] = df_after_signal[df_after_signal['Low'] <= sl_val].index[0].date().strftime('%Y-%m-%d')

            else:
                if max_pct_from_entry < (R['trailing_min_pct'] * 100):
                    trade['Status'] = "OPEN"
                elif max_pct_from_entry < (R['target_pct'] * 100):
                    trade['Status'] = "TRAILING"
                else:
                    trade['Status'] = "PROFIT"

                if (today - sig_date).days >= R['max_hold_days']:
                    trade['Status'] = "TIMEOUT"
                    trade['Exit_Date'] = today.strftime('%Y-%m-%d')
                else:
                    trade['Exit_Date'] = ''

            all_historical_trades.append(trade)

        except Exception as e:
            print(f"Error processing {stock}: {e}", flush=True)
            trade['Status'] = 'OPEN'
            trade['PCT_FROM_ENTRY'] = 0.0
            all_historical_trades.append(trade)

    # PHASE 3: FIRST BREAKOUT SCAN
    print(f"\n[PHASE 3] Scanning for FIRST breakout...", flush=True)
    live_signals_pool = []
    open_trade_stocks = {r['Stock_Name'] for r in all_historical_trades if r.get('Status') in ['OPEN', 'TRAILING']}

    for stock in vip_stocks:
        if stock in open_trade_stocks: continue
        try:
            df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=False)
            if df.empty or len(df) < 35: continue
            df = build_indicators(df)
            total_rows = len(df)
            last_trading_date_str = df.index[-1].strftime('%Y-%m-%d')
            start_idx = max(21, total_rows - R['lookback_trading_days'])
            breakout_idx, breakout_level = find_first_breakout(df, start_idx, total_rows)

            if breakout_idx is not None:
                row_sig = df.iloc[breakout_idx]
                sig_date_str = df.index[breakout_idx].strftime('%Y-%m-%d')
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
                    'Exit_Date': '', 'Status': 'OPEN', 'PCT_FROM_ENTRY': 0.0
                })
        except: continue

    # PHASE 4: SYNC
    try:
        ws_active.clear()
        master_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'Target_Price', 'StopLoss_Price', 'Exit_Date', 'Status', 'PCT_FROM_ENTRY']
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

    print(f"\n=== V18.7 COMPLETE ===", flush=True)

if __name__ == "__main__":
    main()
