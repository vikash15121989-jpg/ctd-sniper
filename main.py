import os
import json
import warnings
from datetime import datetime, timezone, timedelta
import pandas as pd
import gspread
import yfinance as yf

warnings.filterwarnings('ignore')

# =====================================================================
# 1. TIMEZONE & GSHEET CONNECTION
# =====================================================================
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== ⚡ FAST BATCH BACKTESTER (50 STOCKS / BATCH) | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheet: CTD_Sniper", flush=True)
except Exception as e:
    print(f"❌ Sheet connection error: {e}")
    exit(1)

# =====================================================================
# 2. WATCHLIST SE STOCKS READ KARNA
# =====================================================================
try:
    ws_watchlist = sh.worksheet("Watchlist")
    raw_stocks = ws_watchlist.col_values(1)
    
    STOCKS = [
        s.strip().upper() + (".NS" if not s.strip().upper().endswith(".NS") else "")
        for s in raw_stocks 
        if s and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME", "NO TARGETS SET", "NO BREAKOUT YET"]
    ]
    STOCKS = list(set(STOCKS))
    print(f"📋 Watchlist se total {len(STOCKS)} stocks mile.\n")
except Exception as e:
    print(f"❌ Watchlist read karne me error: {e}")
    exit(1)

# =====================================================================
# 3. SINGLE STOCK STRATEGY EVALUATOR
# =====================================================================
def evaluate_stock_strategy(df_stock, symbol):
    if df_stock.empty or len(df_stock) < 60:
        return []

    df = df_stock.copy()
    df['Range'] = df['High'] - df['Low']
    trades = []
    i = 20
    total_rows = len(df)

    while i < total_rows - 15:
        prev_10_max_vol = df['Volume'].iloc[i-10:i].max()
        prev_10_max_range = df['Range'].iloc[i-10:i].max()
        
        curr_vol = df['Volume'].iloc[i]
        curr_range = df['Range'].iloc[i]
        curr_close = df['Close'].iloc[i]
        curr_open = df['Open'].iloc[i]
        
        # Condition 1: Expansion Candle
        if curr_vol > prev_10_max_vol and curr_range > prev_10_max_range and curr_close > curr_open:
            anchor_idx = i
            anchor_vol = curr_vol
            anchor_range = curr_range
            
            dry_days = 0
            base_max_vol = 0
            entry_triggered = False
            entry_idx = -1
            
            # Condition 2: Dry-up Phase Check (2 to 12 days)
            for j in range(anchor_idx + 1, min(anchor_idx + 12, total_rows - 1)):
                post_vol = df['Volume'].iloc[j]
                post_range = df['Range'].iloc[j]
                
                if post_vol <= (anchor_vol * 0.30) and post_range <= anchor_range:
                    dry_days += 1
                    if post_vol > base_max_vol:
                        base_max_vol = post_vol
                else:
                    if dry_days >= 2 and post_vol > base_max_vol:
                        entry_triggered = True
                        entry_idx = j + 1
                        break
                    else:
                        break
            
            # Condition 3: Trade Result Track (+10% Target / -5% SL)
            if entry_triggered and entry_idx < total_rows:
                entry_date = df.index[entry_idx]
                entry_price = round(float(df['Open'].iloc[entry_idx]), 2)
                target_price = round(entry_price * 1.10, 2)
                sl_price = round(entry_price * 0.95, 2)
                
                result = 'EXPIRED'
                exit_price = entry_price
                exit_date = entry_date
                
                for k in range(entry_idx, min(entry_idx + 30, total_rows)):
                    high_p = float(df['High'].iloc[k])
                    low_p = float(df['Low'].iloc[k])
                    
                    if low_p <= sl_price:
                        result = 'STOP_LOSS'
                        exit_price = sl_price
                        exit_date = df.index[k]
                        break
                    elif high_p >= target_price:
                        result = 'TARGET'
                        exit_price = target_price
                        exit_date = df.index[k]
                        break
                
                if result in ['TARGET', 'STOP_LOSS']:
                    trades.append({
                        'Stock': symbol.replace('.NS', ''),
                        'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                        'Entry_Price': entry_price,
                        'Exit_Date': exit_date.strftime('%Y-%m-%d'),
                        'Exit_Price': exit_price,
                        'Result': result,
                        'PnL_%': 10.0 if result == 'TARGET' else -5.0
                    })
                i = entry_idx + 10
                continue
        i += 1

    return trades

# =====================================================================
# 4. BATCH PROCESSING (50 STOCKS AT A TIME)
# =====================================================================
BATCH_SIZE = 50
all_trades = []

print(f"🚀 Starting Fast Batch Processing (Batch Size: {BATCH_SIZE})...\n")

for b_idx in range(0, len(STOCKS), BATCH_SIZE):
    batch_stocks = STOCKS[b_idx : b_idx + BATCH_SIZE]
    print(f"📦 Processing Batch {b_idx//BATCH_SIZE + 1} ({len(batch_stocks)} stocks)...", end=" ", flush=True)
    
    try:
        # Bulk Download for 50 Stocks in 1 API Call
        batch_data = yf.download(batch_stocks, start='2023-01-01', end='2026-09-01', group_by='ticker', progress=False)
        
        for sym in batch_stocks:
            try:
                if len(batch_stocks) == 1:
                    df_sym = batch_data.dropna()
                else:
                    if sym in batch_data and not batch_data[sym].empty:
                        df_sym = batch_data[sym].dropna()
                    else:
                        continue
                
                stock_trades = evaluate_stock_strategy(df_sym, sym)
                all_trades.extend(stock_trades)
            except Exception:
                continue
        print("Done ✅")
    except Exception as e:
        print(f"Error ❌ ({e})")

# =====================================================================
# 5. FINAL REPORT
# =====================================================================
print("\n==========================================================")
print("🏆 FAST BACKTEST SUMMARY REPORT")
print("==========================================================")

if all_trades:
    final_df = pd.DataFrame(all_trades)
    total_trades = len(final_df)
    win_trades = len(final_df[final_df['Result'] == 'TARGET'])
    loss_trades = len(final_df[final_df['Result'] == 'STOP_LOSS'])
    win_rate = (win_trades / total_trades) * 100
    net_pnl_pct = (win_trades * 10.0) - (loss_trades * 5.0)

    print(f"Total Watchlist Stocks Evaluated : {len(STOCKS)}")
    print(f"Total Trades Triggered          : {total_trades}")
    print(f"Winning Trades (+10%)           : {win_trades}")
    print(f"Losing Trades (-5%)             : {loss_trades}")
    print(f"Strategy Win Rate               : {win_rate:.2f}%")
    print(f"Net Cumulative Return           : +{net_pnl_pct:.2f}%")
    print("==========================================================\n")
    print("📋 Trade Log:")
    print(final_df.to_string(index=False))
else:
    print("❌ Is timeframe me is criteria ka koi trade nahi mila.")
