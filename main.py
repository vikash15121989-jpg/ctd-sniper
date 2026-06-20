import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== RS BEATER V25 - LIFETIME PRO BACKTESTER (TOP 4 SHARES) ===")

# Target specific 4 shares requested by the user
TARGET_STOCKS = ['NETWEB', 'GOKEX', 'IDEA', 'LTF']

R = {
    'fixed_target_pct': 6.0,       # Targeted 6% profit
    'fixed_sl_pct': 3.0,           # Strict 3% risk management
    'time_stop_days': 10,          # 10 Days time stop
    'cooldown_days': 5,            
}

def calculate_custom_indicators(df):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    df['Low_Min_10D'] = df['Low'].shift(1).rolling(window=10).min()
    return df

def check_custom_smart_money_pattern(df, idx):
    if idx < 20: return False
    row_today = df.iloc[idx]
    row_prev2 = df.iloc[idx-2]
    
    stop_hunt = (row_today['Low'] < row_today['Low_Min_10D']) and (row_today['Close'] > row_today['Low_Min_10D'])
    price_falling_or_flat = df['Close'].iloc[idx] <= df['Close'].iloc[idx-10]
    rsi_rising = df['RSI'].iloc[idx] > df['RSI'].iloc[idx-10]
    hidden_accumulation = price_falling_or_flat and rsi_rising
    fvg_detected = row_today['Low'] > row_prev2['High']
    
    if stop_hunt or (hidden_accumulation and fvg_detected):
        candle_range = row_today['High'] - row_today['Low']
        if candle_range > 0:
            lower_wick = min(row_today['Open'], row_today['Close']) - row_today['Low']
            if (lower_wick / candle_range) >= 0.35:
                return True
    return False

lifetime_perf = {}

for stock in TARGET_STOCKS:
    print(f"Fetching LIFETIME data for {stock}...")
    ticker = f"{stock}.NS"
    
    # Downloading entire history since listing ('max')
    df = yf.download(ticker, period="max", progress=False, auto_adjust=True)
    if df.empty or len(df) < 50:
        print(f"Data not found for {stock}")
        continue
        
    if isinstance(df.columns, pd.MultiIndex): 
        df.columns = df.columns.get_level_values(0)
        
    df = calculate_custom_indicators(df)
    
    # Track listing and end dates
    listing_date = pd.to_datetime(df.index[0]).date()
    today_date = pd.to_datetime(df.index[-1]).date()
    total_years = max(round((today_date - listing_date).days / 365.25, 1), 1.0)
    
    wins = 0
    losses = 0
    time_exits = 0
    total_trades = 0
    
    in_position = False
    entry_price = 0
    target_price = 0
    sl_price = 0
    entry_idx = 0
    cooldown_until_idx = 0
    
    # Standard backtest execution through entire lifecycle
    for i in range(20, len(df)):
        current_row = df.iloc[i]
        
        if in_position:
            # Trailing logic at 3% to lock risk
            current_max_profit = ((current_row['High'] / entry_price) - 1) * 100
            current_sl = sl_price
            if current_max_profit >= 3.0:
                current_sl = entry_price

            sl_hit = current_row['Low'] <= current_sl
            target_hit = current_row['High'] >= target_price
            days_held = i - entry_idx
            
            if sl_hit and target_hit:
                losses += 1; total_trades += 1; in_position = False; cooldown_until_idx = i + R['cooldown_days']
            elif target_hit:
                wins += 1; total_trades += 1; in_position = False; cooldown_until_idx = i + R['cooldown_days']
            elif sl_hit:
                losses += 1; total_trades += 1; in_position = False; cooldown_until_idx = i + R['cooldown_days']
            elif days_held >= R['time_stop_days']:
                time_exits += 1; total_trades += 1; in_position = False; cooldown_until_idx = i + R['cooldown_days']
                
        else:
            if i < cooldown_until_idx: continue
            if check_custom_smart_money_pattern(df, i):
                in_position = True
                entry_price = current_row['Close']
                target_price = entry_price * (1 + (R['fixed_target_pct'] / 100))
                sl_price = entry_price * (1 - (R['fixed_sl_pct'] / 100))
                entry_idx = i

    lifetime_perf[stock] = {
        'Listing_Date': listing_date,
        'Data_Years': total_years,
        'Total_Trades': total_trades,
        'Wins': wins,
        'Losses': losses + time_exits, # Treating Time Stops as non-wins for safety
        'Win_Rate_%': round((wins / total_trades * 100), 1) if total_trades > 0 else 0,
        'Avg_Trades_Per_Year': round(total_trades / total_years, 1)
    }

# Print Final Comparative Report
print("\n" + "="*85)
print("📊 FINAL LIFETIME HOLY GRAIL REPORT (LISTING TO TILL DATE) 📊")
print("="*85)
df_final = pd.DataFrame.from_dict(lifetime_perf, orient='index').reset_index().rename(columns={'index':'Stock'})
print(df_final.to_string(index=False))
print("="*85)
