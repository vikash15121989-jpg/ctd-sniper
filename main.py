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
LOOKBACK_PATTERN = 5
# ============================

print("=== SINGLE STOCK DNA - ONLY PRICE + VOLUME ===", flush=True)

# Google Sheets
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# FIX: Sheet nahi mile to bana dega
def get_or_create_ws(sh, title):
    try: 
        return sh.worksheet(title)
    except: 
        print(f"Sheet '{title}' nahi mili. Nayi bana raha hu...", flush=True)
        return sh.add_worksheet(title=title, rows=10000, cols=30)

ws_output = get_or_create_ws(sh, "STOCK_DNA") # YEH LINE BADLI HAI

# Stock ka naam Watchlist se uthao
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper().replace('.NS','') for s in stocks if s.strip()]
STOCK = stocks[0] if stocks else "MANKIND"

print(f"Stock: {STOCK}", flush=True)

# 1. DATA DOWNLOAD
df = yf.download(f"{STOCK}.NS", period="max", progress=False, auto_adjust=True)
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
print(f"Data: {df.index[0].date()} to {df.index[-1].date()} | {len(df)} days", flush=True)

# 2. STEP 1 - JAB BHI 10% MOVE AAYA, USSE PEHLE KYA THA
success_moves = []

for i in range(LOOKBACK_PATTERN, len(df) - LOOKFORWARD_DAYS):
    entry_price = df['Close'].iloc[i]
    target_hit = False
    sl_hit = False
    days_to_target = None
    
    for j in range(i+1, i+LOOKFORWARD_DAYS+1):
        if df['Low'].iloc[j] <= entry_price * (1 - STOP_LOSS_PCT/100):
            sl_hit = True
            break
        if df['High'].iloc[j] >= entry_price * (1 + MOVE_PCT/100):
            target_hit = True
            days_to_target = j - i
            break
    
    if target_hit and not sl_hit:
        window = df.iloc[i-LOOKBACK_PATTERN:i]
        today = df.iloc[i]
        
        pattern = {
            'Signal_Date': df.index[i].strftime('%Y-%m-%d'),
            'Entry_Close': round(entry_price, 2),
            'Days_to_10pct': days_to_target,
            'Move_Achieved': round((df['High'].iloc[i+1:i+LOOKFORWARD_DAYS+1].max() / entry_price - 1) * 100, 1),
            
            # PRICE PATTERN
            'High_5D': round(window['High'].max(), 2),
            'Low_5D': round(window['Low'].min(), 2),
            'Range_5D_Pct': round((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100, 2),
            'Close_5D_Change': round((window['Close'].iloc[-1] / window['Close'].iloc[0] - 1) * 100, 2),
            'Higher_Lows': int((window['Low'].diff() > 0).sum()),
            'Higher_Highs': int((window['High'].diff() > 0).sum()),
            'Green_Candles': int((window['Close'] > window['Open']).sum()),
            'Close_Above_Open_5D': int((window['Close'] > window['Open']).all()),
            'Inside_Day': int(window['High'].iloc[-1] < window['High'].iloc[-2] and window['Low'].iloc[-1] > window['Low'].iloc[-2]),
            
            # VOLUME PATTERN
            'Vol_Today': int(today['Volume']),
            'Vol_Avg_5D': int(window['Volume'].mean()),
            'Vol_Ratio': round(today['Volume'] / window['Volume'].mean(), 2) if window['Volume'].mean() > 0 else 0,
            'Vol_Dry_Days': int((window['Volume'] < window['Volume'].mean() * 0.8).sum()),
            'Vol_Increasing': int((window['Volume'].diff() > 0).sum()),
            'Vol_Spike': int(today['Volume'] > window['Volume'].max() * 1.2),
            'Vol_Contraction': int(window['Volume'].iloc[-1] < window['Volume'].iloc[0] * 0.7),
            
            # COMBINED
            'Price_Vol_Breakout': int(today['Close'] > window['High'].max() and today['Volume'] > window['Volume'].mean()),
            'Tight_Range': int(((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100) < 3.0),
        }
        success_moves.append(pattern)

df_success = pd.DataFrame(success_moves)
print(f"\nTotal 10%+ moves found in {STOCK}: {len(df_success)}", flush=True)

# 3. SHEET ME SAVE KARO
ws_output.clear()
if df_success.empty:
    ws_output.update([['Stock', 'Status'], [STOCK, 'No 10% moves found']])
    print("Is stock me 10% move nahi mila", flush=True)
else:
    ws_output.update([df_success.columns.values.tolist()] + df_success.values.tolist())
    print(f"\n[SUCCESS] {len(df_success)} patterns saved to 'STOCK_DNA' sheet", flush=True)
    
    print("\n" + "="*60, flush=True)
    print(f"{STOCK} KA COMMON DNA - 10% MOVE SE PEHLE:", flush=True)
    print("="*60, flush=True)
    
    summary_cols = ['Range_5D_Pct', 'Vol_Ratio', 'Higher_Lows', 'Green_Candles', 'Tight_Range', 'Vol_Dry_Days']
    print(df_success[summary_cols].describe().loc[['mean', '25%', '50%', '75%']], flush=True)
    
    print(f"\nTYPICAL PATTERN:", flush=True)
    print(f"Range_5D_Pct: {df_success['Range_5D_Pct'].median():.1f}%", flush=True)
    print(f"Vol_Ratio: {df_success['Vol_Ratio'].median():.2f}", flush=True)
    print(f"Higher_Lows: {df_success['Higher_Lows'].median():.0f} days out of 5", flush=True)
    print(f"Green_Candles: {df_success['Green_Candles'].median():.0f} days out of 5", flush=True)
    print(f"Tight_Range: {df_success['Tight_Range'].sum()} times out of {len(df_success)}", flush=True)
    print(f"Vol_Dry_Days: {df_success['Vol_Dry_Days'].median():.0f} days out of 5", flush=True)

print("\n=== DNA EXTRACTION COMPLETE ===", flush=True)
