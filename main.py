import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

print("=== STATEFUL TRADING ENGINE V16.7: PRODUCTION READY ===", flush=True)
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
    'cooldown_days': 10,
    'min_wr_for_vip': 0.50,
    'vip_min_trades': 4,
    'batch_size': 50,
    'max_workers': 15
}

# सटीक काउंटर्स डिक्शनरी
DEBUG = {
    'total_download': 0, 'download_fail': 0, 'rejected_by_rules': 0, 'nan_skip': 0,
    'below_ema': 0, 'no_breakout': 0, 'weak_vol': 0, 'red_candle': 0,
    'below_min_price': 0, 'setups_found': 0, 'cooldown_skip': 0
}

def get_or_create_ws(sh, title, rows=1000, cols=10):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
ws_active = get_or_create_ws(sh, "ACTIVE_TRADES")
ws_cooldown = get_or_create_ws(sh, "COOLDOWN_TRACKER")

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
    
    # इंडेक्स से टाइमज़ोन हटाना ज़रूरी है ताकि कैलकुलेशन में एरर न आए
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
        
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) < 35: return df

    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    return df

def check_breakout_signal(df, idx, debug_mode=True):
    global DEBUG
    if idx < 1: return False
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]

    if pd.isna(row['EMA_50']) or pd.isna(row['Breakout_High_20D']) or pd.isna(row['Vol_Multiple']):
        if debug_mode: DEBUG['nan_skip'] += 1
        return False

    is_green = row['Close'] > row['Open']
    if row['Close'] < row['EMA_50']:
        if debug_mode: DEBUG['below_ema'] += 1
        return False

    breakout_level = row['Breakout_High_20D']
    fresh_breakout = (row['Close'] > breakout_level) and (row_prev['Close'] <= breakout_level or row['Open'] > breakout_level)
    good_volume = row['Vol_Multiple'] > 1.2

    if not fresh_breakout:
        if debug_mode: DEBUG['no_breakout'] += 1
        return False
    if not good_volume:
        if debug_mode: DEBUG['weak_vol'] += 1
        return False
    if not is_green:
        if debug_mode: DEBUG['red_candle'] += 1
        return False

    if debug_mode: DEBUG['setups_found'] += 1
    return True

# ===== 3. BACKTEST =====
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
                sl_hit = f_row['Low'] <= sl
                tgt_hit = f_row['High'] >= tgt
                if sl_hit:
                    status = "LOSS"
                    break
                elif tgt_hit:
                    status = "WIN"
                    break
                exit_idx += 1

            signals.append({
                "Ticker": ticker,
                "Entry_Date": df.index[idx],
                "Result": status
            })
            idx = exit_idx + R['cooldown_days']
        else:
            idx += 1
    return signals

# ===== 4. THREADING FOR VIP BUILD =====
def build_vip_for_stock(stock):
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if df.empty or len(df) < 40:
            return "DOWNLOAD_FAIL"

        signals = run_backtest_for_ticker(stock, df, debug_mode=False)

        if len(signals) >= R['vip_min_trades']:
            wins = sum(1 for s in signals if s['Result'] == 'WIN')
            wr = wins / len(signals)
            if wr >= R['min_wr_for_vip']:
                return {'Stock': stock, 'Win_Rate_%': round(wr*100,1), 'Trades': len(signals), 'Wins': wins}
        return "RULE_REJECT" # डाउनलोड हुआ लेकिन फिल्टर पास नहीं हुआ
    except:
        return "DOWNLOAD_FAIL"

# ===== 5. MAIN =====
def main():
    global DEBUG

    # PHASE 1: VIP UNIVERSE
    try:
        vip_rows = ws_filter.get_all_records()
        vip_stocks = [r['Stock'] for r in vip_rows if r.get('Stock')]
    except Exception as e:
        print(f"VIP sheet read error, rebuilding: {e}", flush=True)
        vip_stocks = []

    if not vip_stocks:
        print(f"[PHASE 1] Building VIP Universe: Min {R['vip_min_trades']} Trades + {R['min_wr_for_vip']*100}% WR", flush=True)
        raw_stocks = ws_watchlist.col_values(1)[1:]
        stocks = sorted(list(set([s.strip().upper().replace(".NS", "") for s in raw_stocks if s.strip()])))

        qualified_stocks = []
        with ThreadPoolExecutor(max_workers=R['max_workers']) as executor:
            futures = {executor.submit(build_vip_for_stock, stock): stock for stock in stocks}
            for i, future in enumerate(as_completed(futures)):
                DEBUG['total_download'] += 1
                result = future.result()
                
                # BUG FIX #1: सटीक काउंटर्स असाइनमेंट
                if isinstance(result, dict):
                    qualified_stocks.append(result)
                elif result == "RULE_REJECT":
                    DEBUG['rejected_by_rules'] += 1
                else:
                    DEBUG['download_fail'] += 1

                if (i+1) % 20 == 0:
                    print(f"VIP Build: {i+1}/{len(stocks)} | Qualified: {len(qualified_stocks)}", flush=True)

        df_vip = pd.DataFrame(qualified_stocks)
        ws_filter.clear()
        if not df_vip.empty:
            df_vip = df_vip.sort_values('Win_Rate_%', ascending=False)
            df_vip = df_vip.astype(object).where(pd.notnull(df_vip), "")
            ws_filter.update('A1', [df_vip.columns.values.tolist()] + df_vip.values.tolist())
        vip_stocks = df_vip['Stock'].tolist() if not df_vip.empty else []

    print(f"🎯 VIP Universe: {len(vip_stocks)} Stocks", flush=True)

    # PHASE 2: LOAD COOLDOWN TRACKER
    try:
        cd_rows = ws_cooldown.get_all_records()
        cooldown_tracker = {r['Stock']: datetime.strptime(r['Last_Exit_Date'], '%Y-%m-%d').date()
                           for r in cd_rows if r.get('Stock') and r.get('Last_Exit_Date')}
    except:
        cooldown_tracker = {}

    # PHASE 3: STATE ENGINE
    try:
        active_rows = ws_active.get_all_records()
        active_trades = {r['Stock_Name']: r for r in active_rows if r.get('Stock_Name')}
    except:
        active_trades = {}

    updated_active_trades = []
    new_cooldowns = {}
    today = datetime.now().date()

    print("\n[STATE CHECK] Updating active positions...", flush=True)
    for stock, trade in list(active_trades.items()):
        try:
            df = yf.download(f"{stock}.NS", period="5d", progress=False, auto_adjust=True)
            if df.empty:
                updated_active_trades.append(trade)
                continue
            
            df = build_indicators(df) # टाइमज़ोन फिक्स यहाँ भी अप्लाई होगा

            last_low = df['Low'].min()
            last_high = df['High'].max()
            sl_val = safe_float(trade.get('StopLoss'))
            tgt_val = safe_float(trade.get('Target'))
            exit_date_obj = today

            if last_low <= sl_val and sl_val > 0:
                print(f"❌ {stock} SL Hit! Cooldown started.", flush=True)
                new_cooldowns[stock] = exit_date_obj
            elif last_high >= tgt_val and tgt_val > 0:
                print(f"🎉 {stock} Target Hit! Cooldown started.", flush=True)
                new_cooldowns[stock] = exit_date_obj
            else:
                updated_active_trades.append(trade)
        except:
            updated_active_trades.append(trade)

    cooldown_tracker.update(new_cooldowns)

    # PHASE 4: SCAN NEW SIGNALS
    print(f"\n[PHASE 2] Scanning {R['lookback_trading_days']}-day window on {len(vip_stocks)} VIP stocks...", flush=True)
    active_stock_names = [t['Stock_Name'] for t in updated_active_trades]
    live_signals_pool = []

    for stock in vip_stocks:
        if stock in active_stock_names: continue

        if stock in cooldown_tracker:
            days_since_exit = (today - cooldown_tracker[stock]).days
            if days_since_exit < R['cooldown_days']:
                DEBUG['cooldown_skip'] += 1
                continue

        try:
            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
            if df.empty or len(df) < 35: continue
            df = build_indicators(df)
            total_rows = len(df)
            start_idx = max(21, total_rows - R['lookback_trading_days'])

            for idx in range(start_idx, total_rows):
                if df.iloc[idx]['Close'] < R['min_price']:
                    DEBUG['below_min_price'] += 1
                    continue

                if check_breakout_signal(df, idx, debug_mode=True):
                    row_sig = df.iloc[idx]
                    sig_date = df.index[idx].strftime('%Y-%m-%d')
                    ep = round(float(row_sig['Close']), 2)
                    sl = round(ep * (1 - R['sl_loss_pct']), 2)
                    tgt = round(ep * (1 + R['target_pct']), 2)

                    live_signals_pool.append({
                        'Stock_Name': stock, 'Signal_Date': sig_date,
                        'Entry_Price': ep, 'StopLoss_Price': sl, 'Target_Price': tgt
                    })

                    updated_active_trades.append({
                        'Stock_Name': stock, 'Entry_Date': sig_date,
                        'Entry_Price': ep, 'Target': tgt, 'StopLoss': sl
                    })
                    break
        except:
            continue

    # DEBUG SUMMARY
    print("\n" + "="*60, flush=True)
    print("DEBUG SUMMARY V16.7", flush=True)
    print("="*60, flush=True)
    for k, v in DEBUG.items():
        print(f"{k}: {v}", flush=True)
    print("="*60, flush=True)

    # PHASE 5: SYNC TO SHEETS
    try:
        ws_active.clear()
        active_headers = ['Stock_Name', 'Entry_Date', 'Entry_Price', 'Target', 'StopLoss']
        if updated_active_trades:
            df_active = pd.DataFrame(updated_active_trades)
            df_active = df_active.reindex(columns=active_headers).fillna("")
            df_active = df_active.astype(object)
            ws_active.update('A1', [active_headers] + df_active.values.tolist())
        else:
            ws_active.update('A1', [active_headers])

        ws_cooldown.clear()
        cd_headers = ['Stock', 'Last_Exit_Date']
        if cooldown_tracker:
            df_cd = pd.DataFrame([{'Stock': k, 'Last_Exit_Date': v.strftime('%Y-%m-%d')} for k, v in cooldown_tracker.items()])
            ws_cooldown.update('A1', [cd_headers] + df_cd.values.tolist())
        else:
            ws_cooldown.update('A1', [cd_headers])

        ws_signals.clear()
        signal_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']
        if live_signals_pool:
            df_sig = pd.DataFrame(live_signals_pool).sort_values(by='Signal_Date', ascending=False)
            df_sig = df_sig.reindex(columns=signal_headers).fillna("")
            df_sig = df_sig.astype(object)
            ws_signals.update('A1', [signal_headers] + df_sig.values.tolist())
            print(f"\n🚀 SUCCESS! {len(df_sig)} Fresh Signals uploaded.", flush=True)
        else:
            ws_signals.update('A1', [signal_headers] + [["No new signals", "", "", "", ""]])
            print("\n⚠️ No new signals today.", flush=True)

    except Exception as e:
        print(f"❌ Sync Error: {str(e)}", flush=True)

    print(f"\n=== V16.7 EXECUTION COMPLETE ===", flush=True)

if __name__ == "__main__":
    main()
                
