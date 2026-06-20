import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ========== CONFIG ==========
MOVE_PCT = 10.0
LOOKFORWARD_DAYS = 20
STOP_LOSS_PCT = 5.0
LOOKBACK_PATTERN = 10
# ============================

print("=== NON-OVERLAPPING DNA EXTRACTOR ===", flush=True)

# Google Sheets
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=50000, cols=40)

# Watchlist se stock uthao
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper().replace('.NS','') for s in stocks if s.strip()]
STOCK = stocks[0] if stocks else "RELIANCE"

print(f"Stock: {STOCK} | Pattern Days: {LOOKBACK_PATTERN}", flush=True)

# 1. DATA
df = yf.download(f"{STOCK}.NS", period="max", progress=False, auto_adjust=True)
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
print(f"Data: {df.index[0].date()} to {df.index[-1].date()}", flush=True)

# 2. NON-OVERLAPPING TRADES NIKALO
trades = []
i = LOOKBACK_PATTERN # Start from 10th day

while i < len(df) - 1: # Jab tak data hai
    entry_price = df['Close'].iloc[i]
    entry_date = df.index[i]
    window = df.iloc[i-LOOKBACK_PATTERN:i]
    today = df.iloc[i]
    
    # Check karo agle 20 din me kya hua
    exit_price = None
    exit_date = None
    result = 'NEUTRAL'
    days_held = 0
    max_move_pct = 0
    min_move_pct = 0
    
    for j in range(i+1, min(i+LOOKFORWARD_DAYS+1, len(df))):
        current_high = df['High'].iloc[j]
        current_low = df['Low'].iloc[j]
        
        # Max/Min move track karo
        max_move_pct = max(max_move_pct, (current_high / entry_price - 1) * 100)
        min_move_pct = min(min_move_pct, (current_low / entry_price - 1) * 100)
        
        # SL hit?
        if current_low <= entry_price * (1 - STOP_LOSS_PCT/100):
            result = 'LOSS'
            exit_price = entry_price * (1 - STOP_LOSS_PCT/100)
            exit_date = df.index[j]
            days_held = j - i
            i = j + 1 # Next trade SL ke agle din se start hoga
            break
            
        # Target hit?
        if current_high >= entry_price * (1 + MOVE_PCT/100):
            result = 'WIN'
            exit_price = entry_price * (1 + MOVE_PCT/100)
            exit_date = df.index[j]
            days_held = j - i
            i = j + 1 # Next trade Target ke agle din se start hoga
            break
    
    # Agar 20 din me na SL na Target, to 20 din baad exit
    if result == 'NEUTRAL':
        if i + LOOKFORWARD_DAYS < len(df):
            exit_price = df['Close'].iloc[i + LOOKFORWARD_DAYS]
            exit_date = df.index[i + LOOKFORWARD_DAYS]
            days_held = LOOKFORWARD_DAYS
            i = i + LOOKFORWARD_DAYS + 1 # 20 din baad next trade
        else:
            break # Data khatam
    
    # Sirf WIN trades ka pattern save karo
    if result == 'WIN':
        trades.append({
            'Entry_Date': entry_date.strftime('%Y-%m-%d'),
            'Entry_Close': round(entry_price, 2),
            'Exit_Date': exit_date.strftime('%Y-%m-%d'),
            'Exit_Price': round(exit_price, 2),
            'Days_Held': days_held,
            'Result': result,
            'PnL_Pct': round((exit_price / entry_price - 1) * 100, 1),
            'Max_Move_Pct': round(max_move_pct, 1), # Entry ke baad max kitna gaya
            'Min_Move_Pct': round(min_move_pct, 1), # Entry ke baad min kitna gaya
            
            # 10 DAY PATTERN BEFORE ENTRY
            'Range_10D_Pct': round((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100, 2),
            'Close_10D_Change': round((window['Close'].iloc[-1] / window['Close'].iloc[0] - 1) * 100, 2),
            'Higher_Lows_10D': int((window['Low'].diff() > 0).sum()),
            'Green_Candles_10D': int((window['Close'] > window['Open']).sum()),
            'Vol_Avg_10D': int(window['Volume'].mean()),
            'Vol_Ratio_10D': round(today['Volume'] / window['Volume'].mean(), 2) if window['Volume'].mean() > 0 else 0,
            'Vol_Dry_Days_10D': int((window['Volume'] < window['Volume'].mean() * 0.8).sum()),
        })
    
    # Agar trade nahi hua to i++ karke next day check karo
    if result == 'NEUTRAL' and days_held == 0:
        i += 1

df_trades = pd.DataFrame(trades)
print(f"\nTotal NON-OVERLAPPING 10% Wins: {len(df_trades)}", flush=True)

# 3. SHEET ME SAVE KARO
ws_output = get_or_create_ws(sh, f"{STOCK}_CLEAN_TRADES")
ws_output.clear()

if df_trades.empty:
    ws_output.update([['Stock', 'Status'], [STOCK, 'No 10% wins found']])
else:
    ws_output.update([df_trades.columns.values.tolist()] + df_trades.values.tolist())
    
    print("\n=== TRADE SUMMARY ===", flush=True)
    print(f"Total Trades: {len(df_trades)}", flush=True)
    print(f"Avg Days Held: {df_trades['Days_Held'].mean():.1f}", flush=True)
    print(f"Avg Max Move: {df_trades['Max_Move_Pct'].mean():.1f}%", flush=True)
    print(f"Max Max Move: {df_trades['Max_Move_Pct'].max():.1f}%", flush=True)
    
    print("\n=== DNA PATTERN ===", flush=True)
    print(f"Range_10D: {df_trades['Range_10D_Pct'].median():.1f}%", flush=True)
    print(f"Vol_Ratio: {df_trades['Vol_Ratio_10D'].median():.2f}", flush=True)
    print(f"Higher_Lows: {df_trades['Higher_Lows_10D'].median():.0f}/10", flush=True)

print("\n=== COMPLETE ===", flush=True)
