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

print("=== STEP 1: UNIVERSE FILTER ENGINE V10.0 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# Setup parameters for testing individual stocks
R = {
    'min_price': 60,
    'max_hold_days': 30,
    'cooldown_days': 15,
    'target_pct': 0.12,     # 12% Target
    'sl_loss_pct': 0.05,     # 5% SL
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=10)

# Nayi sheet jahan 50%+ win rate wale stocks save honge
ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")

# ===== 2. INDICATORS =====
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

# ===== 3. READ & CLEAN 500 STOCKS =====
raw_stocks = ws_watchlist.col_values(1)[1:]
stocks = []
for s in raw_stocks:
    cleaned = s.strip().upper().replace("$", "")
    if cleaned and cleaned not in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks.append(cleaned)
stocks = sorted(list(set(stocks)))

print(f"Analyzing {len(stocks)} stocks to filter 50%+ Win Rate setups...", flush=True)

qualified_universe = []

# ===== 4. INDIVIDUAL STOCK CRUNCHING =====
for count, stock in enumerate(stocks, 1):
    try:
        ticker_formatted = f"{stock}.NS"
        df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 40: continue
        
        df = build_indicators(df)
        total_rows = len(df)
        idx = 21
        
        stock_wins = 0
        stock_losses = 0
        stock_trades = 0
        
        while idx < total_rows:
            row = df.iloc[idx]
            if row['Close'] < R['min_price']:
                idx += 1
                continue
                
            if check_signal(df, idx):
                entry_price = row['Close']
                target_price = entry_price * (1 + R['target_pct'])
                sl_price = entry_price * (1 - R['sl_loss_pct'])
                
                exit_idx = idx + 1
                status = "TIMEOUT"
                
                while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                    f_row = df.iloc[exit_idx]
                    if f_row['High'] >= target_price:
                        status = "WIN"
                        break
                    elif f_row['Low'] <= sl_price:
                        status = "LOSS"
                        break
                    exit_idx += 1
                
                stock_trades += 1
                if status == "WIN":
                    stock_wins += 1
                elif status == "LOSS":
                    stock_losses += 1
                    
                idx = exit_idx + R['cooldown_days']
            else:
                idx += 1
        
        # Win Rate calculation
        if stock_trades >= 3: # Kam se kam 3 signals bane ho pichle 1 saal me tabhi select karenge
            win_rate = round((stock_wins / stock_trades) * 100, 1)
            
            # CRITICAL FILTER: Sirf 50%+ Win Rate wale shares select honge
            if win_rate >= 50.0:
                print(f" [{count}/{len(stocks)}] PASS: {stock} -> Win Rate: {win_rate}% ({stock_trades} Trades)", flush=True)
                qualified_universe.append({
                    'Stock': stock,
                    'Win_Rate_%': win_rate,
                    'Total_Trades': stock_trades,
                    'Wins': stock_wins,
                    'Losses': stock_losses
                })
        
        if count % 20 == 0:
            time.sleep(1) # Frequency control to prevent Yahoo block
            
    except Exception:
        continue

# ===== 5. EXPORT THE GOLDEN JARR (50%+ WIN RATE SHEET) =====
if not qualified_universe:
    print("\n⚠️ Alert: Ek bhi share 50% win rate match nahi kar paya criteria se.", flush=True)
else:
    df_filter = pd.DataFrame(qualified_universe)
    df_filter = df_filter.sort_values(by='Win_Rate_%', ascending=False) # Top accuracy waale sabse upar
    
    ws_filter.clear()
    ws_filter.update([df_filter.columns.values.tolist()] + df_filter.values.tolist())
    
    print("\n=======================================================")
    print(f"📢 SUCCESS: {len(df_filter)} STOCKS SAVED WITH 50%+ WIN RATE! 📢")
    print("=======================================================")
    print("Aapki Google Sheet me 'HIGH_WINRATE_STOCKS' tab update ho gaya hai.")
    print("Ab hum agle run me sirf inhi चुनिंदा (selected) shares par trade karenge.", flush=True)

print(f"\n=== FILTER RUN COMPLETE ===", flush=True)
