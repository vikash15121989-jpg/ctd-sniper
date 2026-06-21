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

print("=== BUG-FIXED LIVE ENGINE V12.5: UNIVERSE LOCK & EXACT 10-DAY TRACER ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP & CONFIGURATION =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_price': 60,
    'max_hold_days': 30,
    'cooldown_days': 15,
    'target_pct': 0.12,     # 12% Target Profit
    'sl_loss_pct': 0.05,     # 5% Stop Loss
    'lookback_trading_days': 10  # Strict 10 trading days back lookup
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=10)

ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")

# ===== 2. TECHNICAL INDICATORS LOGIC =====
def build_indicators(df):
    if len(df) < 30: return df
    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    return df

def check_signal(df, idx):
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    
    if row['Close'] < row['EMA_50']: return False
    
    fresh_breakout = row['Close'] > row['Breakout_High_20D'] and row_prev['Close'] <= row_prev['Breakout_High_20D']
    good_volume = row['Vol_Multiple'] > 1.5
    
    if fresh_breakout and good_volume and (row['Close'] > row['Open']):
        return True
    return False

# ===== 3. PHASE 1: FILTER 50%+ WIN RATE STOCKS =====
raw_stocks = ws_watchlist.col_values(1)[1:]
stocks = []
for s in raw_stocks:
    cleaned = s.strip().upper().replace("$", "")
    if cleaned and cleaned not in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks.append(cleaned)
stocks = sorted(list(set(stocks)))

print(f"\n[PHASE 1] Checking 1-Year history of {len(stocks)} stocks for 50%+ Win Rate...", flush=True)

qualified_stocks = []

for count, stock in enumerate(stocks, 1):
    try:
        ticker_formatted = f"{stock}.NS"
        df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 40: continue
        
        df = build_indicators(df)
        total_rows = len(df)
        idx = 21
        
        w, l, t = 0, 0, 0
        
        while idx < total_rows:
            row = df.iloc[idx]
            if row['Close'] < R['min_price']:
                idx += 1
                continue
                
            if check_signal(df, idx):
                entry_p = row['Close']
                tgt_p = entry_p * (1 + R['target_pct'])
                sl_p = entry_p * (1 - R['sl_loss_pct'])
                
                exit_idx = idx + 1
                status = "TIMEOUT"
                while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                    f_row = df.iloc[exit_idx]
                    if f_row['High'] >= tgt_p:
                        status = "WIN"
                        break
                    elif f_row['Low'] <= sl_p:
                        status = "LOSS"
                        break
                    exit_idx += 1
                
                t += 1
                if status == "WIN": w += 1
                elif status == "LOSS": l += 1
                idx = exit_idx + R['cooldown_days']
            else:
                idx += 1
                
        if t >= 3:
            win_rate = round((w / t) * 100, 1)
            if win_rate >= 50.0:
                qualified_stocks.append({'Stock': stock, 'Win_Rate_%': win_rate, 'Total_Trades': t})
                
        if count % 30 == 0: time.sleep(0.5)
    except Exception:
        continue

if not qualified_stocks:
    print("⚠️ Alert: No stocks matched 50%+ Win Rate criteria. Retaining from Sheet database...", flush=True)
    try: vip_stocks = [r[0] for r in ws_filter.get_all_values()[1:] if r]
    except: vip_stocks = []
else:
    df_vip_list = pd.DataFrame(qualified_stocks).sort_values(by='Win_Rate_%', ascending=False)
    ws_filter.clear()
    ws_filter.update([df_vip_list.columns.values.tolist()] + df_vip_list.values.tolist())
    vip_stocks = df_vip_list['Stock'].tolist()
    print(f"🎯 VIP Universe Locked! {len(vip_stocks)} stocks updated in 'HIGH_WINRATE_STOCKS'.", flush=True)

# ===== 4. PHASE 2: STRICT LOOKBACK ENGINE FOR VIP UNIVERSE =====
if not vip_stocks:
    print("\n🛑 Live Signal Engine stopped: VIP Universe is empty.", flush=True)
else:
    print(f"\n[PHASE 2] Fetching exact recent signals for {len(vip_stocks)} VIP Stocks...", flush=True)
    live_signals_pool = []
    
    for stock in vip_stocks:
        try:
            ticker_formatted = f"{stock}.NS"
            # FIX: Pura 1y manga rahe hain taaki indicators (20D High/50 EMA) bilkul backtest wale match ho!
            df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if df.empty or len(df) < 40: continue
            
            df = build_indicators(df)
            total_rows = len(df)
            
            # Pure dataset mein se strict last 10 rows ko window map karenge
            start_lookback_idx = max(21, total_rows - R['lookback_trading_days'])
            
            for idx in range(start_lookback_idx, total_rows):
                if df.iloc[idx]['Close'] < R['min_price']: continue
                
                if check_signal(df, idx):
                    row_signal = df.iloc[idx]
                    sig_date = df.index[idx].strftime('%Y-%m-%d')
                    entry_price = round(row_signal['Close'], 2)
                    stop_loss = round(entry_price * (1 - R['sl_loss_pct']), 2)
                    target_level = round(entry_price * (1 + R['target_pct']), 2)
                    
                    live_signals_pool.append({
                        'Stock_Name': stock,
                        'Signal_Date': sig_date,
                        'Entry_Price': entry_price,
                        'StopLoss_Price': stop_loss,
                        'Target_Price': target_level
                    })
            time.sleep(0.01)
        except Exception:
            continue

    # ===== 5. EXPORT LIVE RECENT SIGNALS TO GOOGLE SHEET =====
    try:
        ws_signals.clear()
        if live_signals_pool:
            df_signals_push = pd.DataFrame(live_signals_pool).sort_values(by='Signal_Date', ascending=False)
            ws_signals.update([df_signals_push.columns.values.tolist()] + df_signals_push.values.tolist())
            print(f"\n🚀 SUCCESS! {len(df_signals_push)} Signals pushed to 'NEW_SIGNALS_TODAY'.", flush=True)
            print(df_signals_push.to_string(index=False))
        else:
            headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']
            ws_signals.update([headers] + [["No signals generated in last 10 trading days.", "", "", "", ""]])
            print("\n⚠️ Alert: Pichle 10 trading dino me ek bhi VIP stock me setup nahi bana.", flush=True)
    except Exception as e:
        print(f"❌ Live Sheet update error: {str(e)}", flush=True)

print(f"\n=== AUTOMATED WORKFLOW V12.5 COMPLETE ===", flush=True)
