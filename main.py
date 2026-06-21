import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== STATEFUL TRADING ENGINE V16.0: ACTIVE TRACKER & RETEST MEMORY ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP & CONFIGURATION =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_price': 60,
    'max_hold_days': 30,
    'target_pct': 0.12,     # 12% Target
    'sl_loss_pct': 0.05,     # 5% Stop Loss
    'lookback_trading_days': 10
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=10)

ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
ws_active = get_or_create_ws(sh, "ACTIVE_TRADES") # Core Memory Tab

# ===== 2. TECHNICAL INDICATORS LOGIC =====
def build_indicators(df):
    if len(df) < 35: return df
    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    return df

def check_breakout_signal(df, idx):
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    if row['Close'] < row['EMA_50']: return False
    
    fresh_breakout = row['Close'] > row['Breakout_High_20D'] and row_prev['Close'] <= row_prev['Breakout_High_20D']
    good_volume = row['Vol_Multiple'] > 1.2
    
    if fresh_breakout and good_volume and (row['Close'] > row['Open']):
        return True
    return False

# ===== 3. PHASE 1: LOAD OR UPDATE VIP UNIVERSE (50%+ WIN RATE) =====
# VIP Universe load karne ke liye pehle HIGH_WINRATE_STOCKS check karenge
try:
    vip_rows = ws_filter.get_all_records()
    vip_stocks = [r['Stock'] for r in vip_rows if 'Stock' in r]
except:
    vip_stocks = []

if not vip_stocks:
    print("[PHASE 1] VIP Universe empty, building from Watchlist...", flush=True)
    raw_stocks = ws_watchlist.col_values(1)[1:]
    stocks = sorted(list(set([s.strip().upper().replace("$", "") for s in raw_stocks if s.strip()])))
    
    qualified_stocks = []
    for stock in stocks:
        try:
            df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
            if df.empty or len(df) < 40: continue
            df = build_indicators(df)
            total_rows = len(df)
            idx = 21
            w, l, t = 0, 0, 0
            while idx < total_rows:
                if check_breakout_signal(df, idx):
                    entry = df.iloc[idx]['Close']
                    tgt, sl = entry * (1 + R['target_pct']), entry * (1 - R['sl_loss_pct'])
                    exit_idx = idx + 1
                    status = "TIMEOUT"
                    while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                        f_row = df.iloc[exit_idx]
                        if f_row['High'] >= tgt: status = "WIN"; break
                        elif f_row['Low'] <= sl: status = "LOSS"; break
                        exit_idx += 1
                    t += 1
                    if status == "WIN": w += 1
                    elif status == "LOSS": l += 1
                    idx = exit_idx + 15
                else: idx += 1
            if t >= 2 and (w / t) >= 0.5:
                qualified_stocks.append({'Stock': stock, 'Win_Rate_%': round((w/t)*100,1)})
        except: continue
    df_vip = pd.DataFrame(qualified_stocks)
    ws_filter.clear()
    ws_filter.update([df_vip.columns.values.tolist()] + df_vip.values.tolist())
    vip_stocks = df_vip['Stock'].tolist()

print(f"🎯 VIP Universe Ready: {len(vip_stocks)} Stocks loaded.", flush=True)

# ===== 4. STATE ENGINE: LOAD EXISTING ACTIVE TRADES =====
try:
    active_rows = ws_active.get_all_records()
    active_trades = {r['Stock_Name']: r for r in active_rows if 'Stock_Name' in r}
except:
    active_trades = {}
    ws_active.update([['Stock_Name', 'Entry_Date', 'Entry_Price', 'Target', 'StopLoss']])

# ===== 5. PHASE 2: TRACK & UPDATE ACTIVE POSITION METRICS =====
updated_active_trades = []
live_signals_pool = []

print("\n[STATE CHECK] Updating current active positions via Live Data...", flush=True)
for stock, trade in list(active_trades.items()):
    try:
        df = yf.download(f"{stock}.NS", period="5d", progress=False, auto_adjust=True)
        if df.empty: 
            updated_active_trades.append(trade) # safe backup
            continue
        
        last_low = df['Low'].min()
        last_high = df['High'].max()
        
        # Check if StopLoss or Target hit
        if last_low <= float(trade['StopLoss']):
            print(f"❌ {stock} hit StopLoss ({trade['StopLoss']})! Position cleared. Retest mode ON.", flush=True)
            continue
        elif last_high >= float(trade['Target']):
            print(f"🎉 {stock} hit Target ({trade['Target']})! Position cleared. Retest mode ON.", flush=True)
            continue
        else:
            # Active hai aur upar ja raha hai, hold memory lock
            updated_active_trades.append(trade)
    except:
        updated_active_trades.append(trade)

# ===== 6. SCAN FOR NEW SIGNALS IN 10-DAY WINDOW FROM HIGH WINRATE LIST =====
print(f"\n[PHASE 2] Scanning 10-day window for new entries from VIP list...", flush=True)
for stock in vip_stocks:
    # Rule: Agar share already enter ho chuka hai aur upar badh raha hai, stop checking duplicate entries!
    if stock in [t['Stock_Name'] for t in updated_active_trades]:
        continue
        
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 35: continue
        
        df = build_indicators(df)
        total_rows = len(df)
        
        # Lookback window calculation
        start_idx = max(21, total_rows - R['lookback_trading_days'])
        
        for idx in range(start_idx, total_rows):
            if df.iloc[idx]['Close'] < R['min_price']: continue
            
            # Rule: Agar dynamic breakout valid hota hai
            if check_breakout_signal(df, idx):
                row_sig = df.iloc[idx]
                sig_date = df.index[idx].strftime('%Y-%m-%d')
                ep = round(row_sig['Close'], 2)
                sl = round(ep * (1 - R['sl_loss_pct']), 2)
                tgt = round(ep * (1 + R['target_pct']), 2)
                
                new_trade = {
                    'Stock_Name': stock,
                    'Signal_Date': sig_date,
                    'Entry_Price': ep,
                    'StopLoss_Price': sl,
                    'Target_Price': tgt
                }
                live_signals_pool.append(new_trade)
                
                # Sheet memory state me position ko instantly lock karo
                updated_active_trades.append({
                    'Stock_Name': stock,
                    'Entry_Date': sig_date,
                    'Entry_Price': ep,
                    'Target': tgt,
                    'StopLoss': sl
                })
                break # Ek stock ke liye is window me ek hi valid fresh signal lock hoga
        time.sleep(0.01)
    except:
        continue

# ===== 7. DATABASE SYNCHRONIZATION =====
try:
    # 1. Update ACTIVE_TRADES memory sheet
    ws_active.clear()
    active_headers = ['Stock_Name', 'Entry_Date', 'Entry_Price', 'Target', 'StopLoss']
    if updated_active_trades:
        df_active = pd.DataFrame(updated_active_trades)
        ws_active.update([active_headers] + df_active[active_headers].values.tolist())
    else:
        ws_active.update([active_headers])

    # 2. Update NEW_SIGNALS_TODAY for current alert sheet
    ws_signals.clear()
    signal_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']
    if live_signals_pool:
        df_sig = pd.DataFrame(live_signals_pool).sort_values(by='Signal_Date', ascending=False)
        ws_signals.update([signal_headers] + df_sig[signal_headers].values.tolist())
        print(f"\n🚀 SUCCESS! {len(df_sig)} Fresh Signal logs uploaded cleanly.", flush=True)
    else:
        ws_signals.update([signal_headers] + [["No new signals generated or retest pending.", "", "", "", ""]])
        print("\n⚠️ Alert: Aaj koi naya breakout setup ya rules filter clear nahi hua.", flush=True)

except Exception as e:
    print(f"❌ Sync Error: {str(e)}", flush=True)

print(f"\n=== AUTOMATED STATE WORKFLOW V16.0 COMPLETE ===", flush=True)
