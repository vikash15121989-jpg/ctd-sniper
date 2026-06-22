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

print("=== STATEFUL TRADING ENGINE V17.5: ONE TRADE PER STOCK ===", flush=True)
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

DEBUG = {
    'total_download': 0, 'download_fail': 0, 'rejected_by_rules': 0, 'nan_skip': 0,
    'below_ema': 0, 'no_breakout': 0, 'weak_vol': 0, 'red_candle': 0,
    'below_min_price': 0, 'setups_found': 0, 'low_liquidity_skip': 0,
    'open_trade_skip': 0 # NEW
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

# ===== 2. INDICATORS & LIQUIDITY CHECK =====
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
    avg_vol = row['Vol_20MA']
    avg_turnover = row['Turnover_20MA']

    if pd.isna(avg_vol) or pd.isna(avg_turnover):
        return False

    if avg_vol >= R['min_avg_volume'] and avg_turnover >= R['min_daily_turnover']:
        return True
    return False

def check_breakout_signal(df, idx, debug_mode=True):
    global DEBUG
    if idx < 1: return False
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]

    if pd.isna(row['EMA_50']) or pd.isna(row['Breakout_High_20D']) or pd.isna(row['Vol_Multiple']):
        if debug_mode: DEBUG['nan_skip'] += 1
        return False

    if not check_liquidity(df, idx):
        if debug_mode: DEBUG['low_liquidity_skip'] += 1
        return False

    is_green = row['Close'] > row['Open']
    if row['Close'] < row['EMA_50']:
        if debug_mode: DEBUG['below_ema'] += 1
        return False

    breakout_level = row['Breakout_High_20D']
    fresh_breakout = (row['Close'] > breakout_level) and (row_prev['Close'] <= breakout_level or row['Open'] > breakout_level)
    good_volume = row['Vol_Multiple'] > 1.2

    if not fresh_breakout or not good_volume or not is_green:
        if not fresh_breakout and debug_mode: DEBUG['no_breakout'] += 1
        elif not good_volume and debug_mode: DEBUG['weak_vol'] += 1
        elif not is_green and debug_mode: DEBUG['red_candle'] += 1
        return False

    if debug_mode: DEBUG['setups_found'] += 1
    return True

# ===== 3. BACKTEST FOR VIP UNIVERSES =====
def run_backtest_for_ticker(ticker, df, debug_mode=False):
    if df is None or len(df) < 40: return []
    df = build_indicators(df)
    signals = []
    idx = 21
    total_rows = len(df)

    while idx < total_rows:
        if df.iloc[idx]['Close'] < R['min_price']:
            idx += 1
            continue

        if check_breakout_signal(df, idx, debug_mode=debug_mode):
            entry = df.iloc[idx]['Close']
            tgt, sl = entry * (1 + R['target_pct']), entry * (1 - R['sl_loss_pct'])
            exit_idx = idx + 1
            status = "TIMEOUT"

            while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                f_row = df.iloc[exit_idx]
                if f_row['Low'] <= sl:
                    status = "LOSS"
                    break
                elif f_row['High'] >= tgt:
                    status = "WIN"
                    break
                exit_idx += 1

            signals.append({"Ticker": ticker, "Result": status})
            idx = exit_idx + 1
        else:
            idx += 1
    return signals

def build_vip_for_stock(stock):
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if df.empty or len(df) < 40: return "DOWNLOAD_FAIL"
        df = build_indicators(df)

        if not check_liquidity(df, len(df)-1):
            return "RULE_REJECT"

        signals = run_backtest_for_ticker(stock, df, debug_mode=False)

        if len(signals) >= R['vip_min_trades']:
            wins = sum(1 for s in signals if s['Result'] == 'WIN')
            wr = wins / len(signals)
            if wr >= R['min_wr_for_vip']:
                return {'Stock': stock, 'Win_Rate_%': round(wr*100,1), 'Trades': len(signals), 'Wins': wins}
        return "RULE_REJECT"
    except:
        return "DOWNLOAD_FAIL"

# ===== 4. MAIN EXECUTION =====
def main():
    global DEBUG
    today = datetime.now().date()

    # --- PHASE 1: 1-WEEK AUTO LOCK LOGIC ---
    print("[PHASE 1] Checking VIP Sheet Lock Status...", flush=True)
    vip_stocks = []
    should_rebuild_vip = False

    try:
        all_vip_data = ws_filter.get_all_values()
        if len(all_vip_data) > 1:
            lock_string = all_vip_data[0][0]
            if lock_string.startswith("LOCK_UNTIL:"):
                lock_date_str = lock_string.split(":")[1].strip()
                lock_date = datetime.strptime(lock_date_str, '%Y-%m-%d').date()

                if today <= lock_date:
                    print(f"🔒 VIP Sheet is LOCKED until {lock_date_str}. Fetching locked stocks.", flush=True)
                    for row in all_vip_data[2:]:
                        if row[0]:
                            vip_stocks.append(row[0])
                else:
                    print("🔓 Lock expired! 1 week is over.", flush=True)
                    should_rebuild_vip = True
            else:
                should_rebuild_vip = True
        else:
            should_rebuild_vip = True
    except Exception as e:
        print(f"Error reading lock status, triggering rebuild: {e}", flush=True)
        should_rebuild_vip = True

    if should_rebuild_vip:
        print("🔄 Running Fresh Re-Backtest with Liquidity Check...", flush=True)
        raw_stocks = ws_watchlist.col_values(1)[1:]
        stocks = sorted(list(set([s.strip().upper().replace(".NS", "") for s in raw_stocks if s.strip()])))

        qualified_stocks = []
        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(build_vip_for_stock, stock): stock for stock in stocks}
            for i, future in enumerate(as_completed(futures)):
                DEBUG['total_download'] += 1
                result = future.result()
                if isinstance(result, dict):
                    qualified_stocks.append(result)
                elif result == "RULE_REJECT":
                    DEBUG['rejected_by_rules'] += 1
                else:
                    DEBUG['download_fail'] += 1

        df_vip = pd.DataFrame(qualified_stocks)
        ws_filter.clear()

        unlock_date_str = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
        lock_header = [f"LOCK_UNTIL:{unlock_date_str}", "", "", ""]
        vip_headers = ['Stock', 'Win_Rate_%', 'Trades', 'Wins']

        if not df_vip.empty:
            df_vip = df_vip.sort_values('Win_Rate_%', ascending=False)
            df_vip = df_vip.astype(object).where(pd.notnull(df_vip), "")
            ws_filter.update('A1', [lock_header, vip_headers] + df_vip.values.tolist())
            vip_stocks = df_vip['Stock'].tolist()
        else:
            ws_filter.update('A1', [lock_header, vip_headers])
            vip_stocks = []

        print(f"🔒 VIP Universe locked for 1 week until: {unlock_date_str}", flush=True)

    print(f"🎯 Total Active VIP Stocks for Scan: {len(vip_stocks)}", flush=True)

    # --- PHASE 2: UNIFIED MASTER TRACKER (ACTIVE TRADES) ---
    print("\n[PHASE 2] Syncing Master Positions...", flush=True)
    try:
        master_rows = ws_active.get_all_records()
        open_trades = {r['Stock_Name']: r for r in master_rows if r.get('Stock_Name') and (not r.get('Status') or r.get('Status') == 'OPEN')}
        all_historical_trades = master_rows
    except Exception as e:
        print(f"Error reading ACTIVE_TRADES sheet: {e}", flush=True)
        open_trades = {}
        all_historical_trades = []

    for stock, trade in list(open_trades.items()):
        try:
            sig_date_str = trade.get('Signal_Date')
            sl_val = safe_float(trade.get('StopLoss_Price'))
            tgt_val = safe_float(trade.get('Target_Price'))

            if not sig_date_str or sl_val <= 0 or tgt_val <= 0:
                continue

            sig_date = datetime.strptime(sig_date_str, '%Y-%m-%d').date()
            entry_start_date = sig_date + timedelta(days=1)

            if entry_start_date > today:
                continue

            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue

            df = build_indicators(df)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)

            df_after_signal = df[df.index.date >= entry_start_date]
            if df_after_signal.empty:
                continue

            trade_status = "OPEN"
            exit_date_str = ""

            for current_date, row in df_after_signal.iterrows():
                day_low = float(row['Low'])
                day_high = float(row['High'])
                date_str = current_date.date().strftime('%Y-%m-%d')

                if day_low <= sl_val and day_high >= tgt_val:
                    trade_status = "LOSS"
                    exit_date_str = date_str
                    break
                elif day_low <= sl_val:
                    trade_status = "LOSS"
                    exit_date_str = date_str
                    break
                elif day_high >= tgt_val:
                    trade_status = "PROFIT"
                    exit_date_str = date_str
                    break

            # TIMEOUT CHECK
            if trade_status == "OPEN":
                days_held = (today - sig_date).days
                if days_held >= R['max_hold_days']:
                    trade_status = "TIMEOUT"
                    exit_date_str = today.strftime('%Y-%m-%d')

            if trade_status!= "OPEN":
                for idx, item in enumerate(all_historical_trades):
                    if item.get('Stock_Name') == stock and (not item.get('Status') or item.get('Status') == 'OPEN') and item.get('Signal_Date') == sig_date_str:
                        all_historical_trades[idx]['Status'] = trade_status
                        all_historical_trades[idx]['Exit_Date'] = exit_date_str
                        print(f"🎯 {stock} status updated to {trade_status} on {exit_date_str}", flush=True)
                        open_trades.pop(stock, None)
                        break

        except Exception as e:
            print(f"Error tracking active stock {stock}: {e}", flush=True)

    # --- PHASE 3: FRESH BREAKOUT SCANNING WITH ONE-TRADE RULE ---
    print(f"\n[PHASE 3] Scanning {R['lookback_trading_days']}-day window...", flush=True)
    live_signals_pool = []

    # 🎯 CRITICAL FIX: Open trades ki list nikal lo
    open_trade_stocks = {r['Stock_Name'] for r in all_historical_trades if r.get('Status') == 'OPEN'}
    print(f"🔒 Currently OPEN trades: {len(open_trade_stocks)} stocks", flush=True)

    for stock in vip_stocks:
        # 🎯 FIX: Agar stock pehle se OPEN hai to skip kar de
        if stock in open_trade_stocks:
            DEBUG['open_trade_skip'] += 1
            continue

        try:
            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
            if df.empty or len(df) < 35: continue
            df = build_indicators(df)
            total_rows = len(df)

            last_trading_date_str = df.index[-1].strftime('%Y-%m-%d')
            start_idx = max(21, total_rows - R['lookback_trading_days'])

            for idx in range(start_idx, total_rows):
                if df.iloc[idx]['Close'] < R['min_price']:
                    DEBUG['below_min_price'] += 1
                    continue

                if check_breakout_signal(df, idx, debug_mode=True):
                    row_sig = df.iloc[idx]
                    sig_date_str = df.index[idx].strftime('%Y-%m-%d')

                    # Duplicate Guard: same date check
                    already_exists = any(h.get('Stock_Name') == stock and h.get('Signal_Date') == sig_date_str for h in all_historical_trades)
                    if already_exists: continue

                    ep = round(float(row_sig['Close']), 2)
                    sl = round(ep * (1 - R['sl_loss_pct']), 2)
                    tgt = round(ep * (1 + R['target_pct']), 2)

                    # RULE 1: NEW_SIGNALS_TODAY - Sirf aaj ka signal
                    if sig_date_str == last_trading_date_str:
                        live_signals_pool.append({
                            'Stock_Name': stock, 'Signal_Date': sig_date_str,
                            'Entry_Price': ep, 'StopLoss_Price': sl, 'Target_Price': tgt
                        })

                    # RULE 2: ACTIVE_TRADES - Master record
                    all_historical_trades.append({
                        'Stock_Name': stock, 'Signal_Date': sig_date_str,
                        'Entry_Price': ep, 'Target_Price': tgt, 'StopLoss_Price': sl,
                        'Exit_Date': '', 'Status': 'OPEN'
                    })

                    # 🎯 FIX: Ek signal milte hi break. Ek stock = ek trade at a time
                    break
        except:
            continue

    # DEBUG SUMMARY PRINT
    print("\n" + "="*60, flush=True)
    print("DEBUG SUMMARY V17.5", flush=True)
    print("="*60, flush=True)
    for k, v in DEBUG.items():
        print(f"{k}: {v}", flush=True)
    print("="*60, flush=True)

    # --- PHASE 4: GLOBAL SYNC WITH CHRONOLOGICAL SORTING ---
    try:
        # 1. Master Tracker Sync (ACTIVE_TRADES)
        ws_active.clear()
        master_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'Target_Price', 'StopLoss_Price', 'Exit_Date', 'Status']
        if all_historical_trades:
            df_master = pd.DataFrame(all_historical_trades)
            df_master['Signal_Date_DT'] = pd.to_datetime(df_master['Signal_Date'])
            df_master = df_master.sort_values(by='Signal_Date_DT', ascending=False).drop(columns=['Signal_Date_DT'])
            df_master = df_master.reindex(columns=master_headers).fillna("")
            df_master = df_master.astype(object)
            ws_active.update('A1', [master_headers] + df_master.values.tolist())
            print("📊 Unified Active Sheet Synced (Recent Dates on Top).", flush=True)
        else:
            ws_active.update('A1', [master_headers])

        # 2. Daily Signals Dashboard Sync (NEW_SIGNALS_TODAY)
        ws_signals.clear()
        signal_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']

        if live_signals_pool:
            df_sig = pd.DataFrame(live_signals_pool)
            df_sig = df_sig.reindex(columns=signal_headers).fillna("")
            df_sig = df_sig.astype(object)
            ws_signals.update('A1', [signal_headers] + df_sig.values.tolist())
            print(f"🚀 SUCCESS! {len(df_sig)} Pure Fresh Signals uploaded to NEW_SIGNALS_TODAY.", flush=True)
        else:
            ws_signals.update('A1', [signal_headers] + [["No new signals today", "", "", "", ""]])
            print("⚠️ No fresh signals found for the last trading day.", flush=True)

    except Exception as e:
        print(f"❌ Sheet Sync Error: {str(e)}", flush=True)

    print(f"\n=== V17.5 EXECUTION COMPLETE ===", flush=True)

if __name__ == "__main__":
    main()
