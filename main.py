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

print("=== AUTOMATED DYNAMIC UNIVERSE & BACKTEST ENGINE V11.0 ===", flush=True)
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
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=15)

ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_live = get_or_create_ws(sh, "LIVE_TRADES_V8_3")
ws_summary = get_or_create_ws(sh, "LIVE_SUMMARY")

# ===== 2. INDICATORS LOGIC =====
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

# ===== 3. PHASE 1: SCANNING ALL 500 STOCKS FOR 50%+ WINRATE =====
raw_stocks = ws_watchlist.col_values(1)[1:]
stocks = []
for s in raw_stocks:
    cleaned = s.strip().upper().replace("$", "")
    if cleaned and cleaned not in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks.append(cleaned)
stocks = sorted(list(set(stocks)))

print(f"\n[PHASE 1] Filtering 50%+ Win Rate stocks out of {len(stocks)} symbols...", flush=True)

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
                qualified_stocks.append({'Stock': stock, 'Win_Rate_%': win_rate})
                
        if count % 25 == 0: time.sleep(0.5)
    except Exception:
        continue

# Save Phase 1 Results to Sheet
if not qualified_stocks:
    print("⚠️ Alert: No stocks crossed the 50% Win Rate mark. Keeping previous setup.", flush=True)
    vip_stocks = []
else:
    df_vip_list = pd.DataFrame(qualified_stocks).sort_values(by='Win_Rate_%', ascending=False)
    ws_filter.clear()
    ws_filter.update([df_vip_list.columns.values.tolist()] + df_vip_list.values.tolist())
    vip_stocks = df_vip_list['Stock'].tolist()
    print(f"🎯 VIP Universe Ready! {len(vip_stocks)} stocks saved in 'HIGH_WINRATE_STOCKS'.", flush=True)

# ===== 4. PHASE 2: DETAILED DATE-WISE BACKTEST ON VIP STOCKS =====
if not vip_stocks:
    print("\n🛑 Phase 2 stopped because VIP Universe is empty.", flush=True)
else:
    print(f"\n[PHASE 2] Running Detailed Date-wise Backtest on {len(vip_stocks)} VIP Stocks...", flush=True)
    detailed_trades = []
    
    for stock in vip_stocks:
        try:
            ticker_formatted = f"{stock}.NS"
            df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if df.empty: continue
            
            df = build_indicators(df)
            total_rows = len(df)
            idx = 21
            
            while idx < total_rows:
                row = df.iloc[idx]
                if row['Close'] < R['min_price']:
                    idx += 1
                    continue
                    
                if check_signal(df, idx):
                    entry_price = row['Close']
                    entry_date = df.index[idx]
                    
                    target_price = entry_price * (1 + R['target_pct'])
                    sl_price = entry_price * (1 - R['sl_loss_pct'])
                    
                    result = "TIMEOUT"
                    exit_date = None
                    exit_price = entry_price
                    
                    exit_idx = idx + 1
                    while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                        future_row = df.iloc[exit_idx]
                        if future_row['High'] >= target_price:
                            result = "WIN"
                            exit_price = target_price
                            exit_date = df.index[exit_idx]
                            break
                        elif future_row['Low'] <= sl_price:
                            result = "LOSS"
                            exit_price = sl_price
                            exit_date = df.index[exit_idx]
                            break
                        exit_price = future_row['Close']
                        exit_date = df.index[exit_idx]
                        exit_idx += 1
                    
                    pnl_pct = round((exit_price / entry_price - 1) * 100, 1)
                    
                    detailed_trades.append({
                        'Stock': stock,
                        'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                        'Entry_Price': round(entry_price, 2),
                        'SL_Level': round(sl_price, 2),
                        'Target_Level': round(target_price, 2),
                        'Status': result,
                        'Exit_Date': exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                        'Exit_Price': round(exit_price, 2),
                        'PnL_%': pnl_pct
                    })
                    idx = exit_idx + R['cooldown_days']
                else:
                    idx += 1
            time.sleep(0.02)
        except Exception:
            continue

    # ===== 5. EXPORT FINAL FILTERED REPORT TO GOOGLE SHEET =====
    if detailed_trades:
        df_final_report = pd.DataFrame(detailed_trades).sort_values(by='Entry_Date', ascending=False)
        
        # Calculate VIP Metrics
        total_vip_trades = len(df_final_report)
        vip_wins = len(df_final_report[df_final_report['Status'] == 'WIN'])
        vip_losses = len(df_final_report[df_final_report['Status'] == 'LOSS'])
        vip_timeouts = len(df_final_report[df_final_report['Status'] == 'TIMEOUT'])
        vip_winrate = round((vip_wins / total_vip_trades) * 100, 1) if total_vip_trades else 0
        
        print("\n=======================================================")
        print("🏆 STRATEGIC VIP REPORT (FILTERED UNIVERSE RUN) 🏆")
        print("=======================================================")
        print(f"Total High-Conviction Signals : {total_vip_trades}")
        print(f"Profitable Trades (Wins)      : {vip_wins}")
        print(f"Controlled Losses             : {vip_losses}")
        print(f"Time Expired Trades           : {vip_timeouts}")
        print(f"Optimized VIP Win Rate        : {vip_winrate}%")
        print("=======================================================\n")
        
        try:
            # 1. Clear & Update LIVE_TRADES_V8_3 (Date-wise results)
            ws_live.clear()
            df_push = df_final_report.fillna("")
            ws_live.update([df_push.columns.values.tolist()] + df_push.values.tolist())
            
            # 2. Update LIVE_SUMMARY
            ws_summary.clear()
            summary_df = pd.DataFrame([{
                'Execution_Date': datetime.now().strftime('%Y-%m-%d'),
                'VIP_Total_Trades': total_vip_trades,
                'VIP_Winrate_%': vip_winrate,
                'Wins': vip_wins,
                'Losses': vip_losses,
                'Timeouts': vip_timeouts
            }])
            ws_summary.update([summary_df.columns.values.tolist()] + summary_df.values.tolist())
            print("=== GSHEET DYNAMIC METRICS UPDATED SUCCESSFULLY ===", flush=True)
        except Exception as e:
            print(f"❌ GSheet update error: {str(e)}", flush=True)
    else:
        print("\n⚠️ Alert: No historical signals triggered inside the filtered VIP universe.", flush=True)

print(f"\n=== MASTER RUN COMPLETE ===", flush=True)
