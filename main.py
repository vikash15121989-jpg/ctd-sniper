import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ========== CONFIG - YAHAN BADAL ==========
STOCK = "MANKIND" # Konsa stock check karna hai
MOVE_PCT = 10.0 # Kitne % move ko success manenge
LOOKFORWARD_DAYS = 20 # Signal ke baad kitne din me 10% aana chahiye
STOP_LOSS_PCT = 5.0 # Kitne % loss pe fail manenge
LOOKBACK_PATTERN = 5 # Pattern ke liye kitne din peeche dekhna hai
# ==========================================

print(f"=== {STOCK} - UC PATTERN BACKTEST ===", flush=True)

# 1. DATA DOWNLOAD
df = yf.download(f"{STOCK}.NS", period="max", progress=False, auto_adjust=True)
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
print(f"Data: {df.index[0].date()} to {df.index[-1].date()} | {len(df)} days", flush=True)

# 2. INDICATORS
df['EMA20'] = df['Close'].ewm(span=20).mean()
df['EMA50'] = df['Close'].ewm(span=50).mean()
df['Vol_Avg20'] = df['Volume'].rolling(20).mean()
df['Range_Pct'] = (df['High'] - df['Low']) / df['Low'] * 100
delta = df['Close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / loss
df['RSI'] = 100 - (100 / (1 + rs))

# 3. STEP 1 - SABHI 10%+ MOVES DHUNDO
success_signals = []
for i in range(20, len(df) - LOOKFORWARD_DAYS):
    entry_price = df['Close'].iloc[i]
    future_high = df['High'].iloc[i+1:i+LOOKFORWARD_DAYS+1].max()
    future_low = df['Low'].iloc[i+1:i+LOOKFORWARD_DAYS+1].min()
    
    pct_up = (future_high / entry_price - 1) * 100
    pct_down = (future_low / entry_price - 1) * 100
    
    # SL pehle hit hua ya target?
    sl_hit_day = None
    target_hit_day = None
    for j in range(i+1, i+LOOKFORWARD_DAYS+1):
        if df['Low'].iloc[j] <= entry_price * (1 - STOP_LOSS_PCT/100):
            sl_hit_day = j
            break
        if df['High'].iloc[j] >= entry_price * (1 + MOVE_PCT/100):
            target_hit_day = j
            break
    
    if target_hit_day and (sl_hit_day is None or target_hit_day < sl_hit_day):
        # Ye successful 10% move tha
        signal_date = df.index[i]
        window = df.iloc[i-LOOKBACK_PATTERN:i] # Pehle ke 5 din
        
        pattern = {
            'Signal_Date': signal_date,
            'Entry_Close': round(entry_price, 2),
            'Days_to_Target': target_hit_day - i,
            'Move_Achieved': round(pct_up, 1),
            
            # PATTERN FEATURES - UC SE PEHLE KYA THA
            'Range_5D': round((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100, 1),
            'Vol_Ratio': round(df['Volume'].iloc[i] / window['Volume'].mean(), 2),
            'Vol_Dry_Days': int((window['Volume'] < window['Volume'].mean() * 0.8).sum()),
            'Above_EMA20_Days': int((window['Close'] > window['EMA20']).sum()),
            'Close_vs_EMA20': round((df['Close'].iloc[i] / df['EMA20'].iloc[i] - 1) * 100, 1),
            'Higher_Lows': int((window['Low'].diff() > 0).sum()),
            'Green_Days': int((window['Close'] > window['Open']).sum()),
            'RSI': round(df['RSI'].iloc[i], 1),
            'Consolidation': int(window['Range_Pct'].max() < 3.0), # 3% se kam range
        }
        success_signals.append(pattern)

df_success = pd.DataFrame(success_signals)
print(f"\nTotal {MOVE_PCT}%+ moves found: {len(df_success)}", flush=True)

if df_success.empty:
    print("Is stock me 10% move ka pattern nahi mila. Dusra stock try kar.", flush=True)
else:
    # 4. COMMON PATTERN NIKALO - KYA COMMON HAI SAB SUCCESS ME?
    print("\n" + "="*60, flush=True)
    print("SUCCESSFUL MOVES KA COMMON PATTERN:", flush=True)
    print("="*60, flush=True)
    print(df_success[['Range_5D', 'Vol_Ratio', 'Above_EMA20_Days', 'RSI', 'Close_vs_EMA20']].describe(), flush=True)
    
    # Median values = Ye typical pattern hai
    median_pattern = {
        'Range_5D': df_success['Range_5D'].median(),
        'Vol_Ratio': df_success['Vol_Ratio'].median(),
        'Vol_Dry_Days': int(df_success['Vol_Dry_Days'].median()),
        'Above_EMA20_Days': int(df_success['Above_EMA20_Days'].median()),
        'RSI_Min': df_success['RSI'].quantile(0.25),
        'RSI_Max': df_success['RSI'].quantile(0.75),
        'Close_vs_EMA20_Min': df_success['Close_vs_EMA20'].quantile(0.25),
        'Close_vs_EMA20_Max': df_success['Close_vs_EMA20'].quantile(0.75),
    }
    print(f"\nTYPICAL PATTERN FOR {STOCK}:", flush=True)
    for k, v in median_pattern.items():
        print(f"{k}: {v}", flush=True)
    
    # 5. STEP 2 - AB POORE HISTORY ME DEKHO YE PATTERN KITNI BAAR AAYA
    print("\n" + "="*60, flush=True)
    print("BACKTESTING SAME PATTERN ON FULL HISTORY:", flush=True)
    print("="*60, flush=True)
    
    all_signals = []
    for i in range(20, len(df) - LOOKFORWARD_DAYS):
        window = df.iloc[i-LOOKBACK_PATTERN:i]
        row = df.iloc[i]
        
        # Check if current pattern matches median pattern
        range_5d = (window['High'].max() - window['Low'].min()) / window['Low'].min() * 100
        vol_ratio = row['Volume'] / window['Volume'].mean()
        vol_dry = (window['Volume'] < window['Volume'].mean() * 0.8).sum()
        above_ema = (window['Close'] > window['EMA20']).sum()
        rsi = row['RSI']
        close_vs_ema20 = (row['Close'] / row['EMA20'] - 1) * 100
        
        # Pattern match condition - 25-75 percentile range
        match = (
            median_pattern['Range_5D'] * 0.7 <= range_5d <= median_pattern['Range_5D'] * 1.3 and
            median_pattern['Vol_Ratio'] * 0.7 <= vol_ratio <= median_pattern['Vol_Ratio'] * 1.3 and
            abs(vol_dry - median_pattern['Vol_Dry_Days']) <= 1 and
            abs(above_ema - median_pattern['Above_EMA20_Days']) <= 1 and
            median_pattern['RSI_Min'] <= rsi <= median_pattern['RSI_Max'] and
            median_pattern['Close_vs_EMA20_Min'] <= close_vs_ema20 <= median_pattern['Close_vs_EMA20_Max']
        )
        
        if match:
            # Pattern mila, ab result check karo
            entry_price = row['Close']
            sl_hit = False
            target_hit = False
            exit_day = None
            exit_reason = None
            
            for j in range(i+1, i+LOOKFORWARD_DAYS+1):
                if df['Low'].iloc[j] <= entry_price * (1 - STOP_LOSS_PCT/100):
                    sl_hit = True
                    exit_day = j
                    exit_reason = 'SL'
                    break
                if df['High'].iloc[j] >= entry_price * (1 + MOVE_PCT/100):
                    target_hit = True
                    exit_day = j
                    exit_reason = 'TARGET'
                    break
            
            if not exit_day: # Time expire
                exit_day = i + LOOKFORWARD_DAYS
                exit_reason = 'TIME'
            
            exit_price = df['Close'].iloc[exit_day]
            pnl_pct = (exit_price / entry_price - 1) * 100
            
            all_signals.append({
                'Signal_Date': df.index[i],
                'Entry': round(entry_price, 2),
                'Exit': round(exit_price, 2),
                'Exit_Reason': exit_reason,
                'PnL_Pct': round(pnl_pct, 1),
                'Result': 'WIN' if target_hit else 'LOSS' if sl_hit else 'NEUTRAL'
            })
    
    df_backtest = pd.DataFrame(all_signals)
    
    if df_backtest.empty:
        print("Pattern match nahi hua poore history me.", flush=True)
    else:
        wins = len(df_backtest[df_backtest['Result'] == 'WIN'])
        losses = len(df_backtest[df_backtest['Result'] == 'LOSS'])
        neutral = len(df_backtest[df_backtest['Result'] == 'NEUTRAL'])
        total = len(df_backtest)
        winrate = round(wins / total * 100, 1)
        avg_win = df_backtest[df_backtest['PnL_Pct'] > 0]['PnL_Pct'].mean()
        avg_loss = df_backtest[df_backtest['PnL_Pct'] < 0]['PnL_Pct'].mean()
        
        print(f"\nTOTAL PATTERN OCCURRENCES: {total}", flush=True)
        print(f"WIN: {wins} | LOSS: {losses} | NEUTRAL: {neutral}", flush=True)
        print(f"WINRATE: {winrate}%", flush=True)
        print(f"Avg Win: +{round(avg_win,1)}% | Avg Loss: {round(avg_loss,1)}%", flush=True)
        print(f"Expectancy: {round((winrate/100 * MOVE_PCT) + ((100-winrate)/100 * avg_loss), 2)}% per trade", flush=True)
        
        print("\nLAST 10 SIGNALS:", flush=True)
        print(df_backtest.tail(10).to_string(index=False), flush=True)

print("\n=== ANALYSIS COMPLETE ===", flush=True)
