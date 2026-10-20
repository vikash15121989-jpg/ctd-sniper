import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os

print("=== 100% SHEET-DRIVEN QUIET ACCUMULATION BACKTESTER ===")

# Google Sheets Connection via GitHub Secrets to fetch Watchlist (Zero Hardcoding)
try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Google Sheet Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = [] # No hardcoded fallback, empty list if sheet fails

def run_backtest(symbol):
    try:
        print(f"Backtesting {symbol}...")
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=365)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 60:
            return []
            
        # Clean and remove duplicate columns
        df.columns = [str(c).strip().title() for c in df.columns]
        df = df.loc[:, ~df.columns.duplicated()]
        
        # Column mapping
        col_map = {}
        for c in df.columns:
            clow = c.lower()
            if 'total traded quantity' in clow or 'quantity' in clow:
                col_map[c] = 'Volume'
            elif 'no. of trades' in clow or 'trades' in clow:
                col_map[c] = 'Trades'
            elif 'close' in clow:
                col_map[c] = 'Close'
            elif 'high' in clow:
                col_map[c] = 'High'
            elif 'low' in clow:
                col_map[c] = 'Low'
            elif 'date' in clow:
                col_map[c] = 'Date'

        df = df.rename(columns=col_map)
        df = df.loc[:, ~df.columns.duplicated()]
        
        required_cols = ['Volume', 'Trades', 'Close', 'High', 'Low', 'Date']
        if not all(col in df.columns for col in required_cols):
            return []

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        # Calculations
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['SMA50'] = df['Close'].rolling(50).mean()
        df['Prev_Close'] = df['Close'].shift(1)
        df['Daily_Return'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close'] * 100
        df['High_20'] = df['High'].rolling(20).max()
        
        trades = []
        i = 50
        n_rows = len(df)
        
        while i < n_rows - 20:
            close = df['Close'].iloc[i]
            high = df['High'].iloc[i]
            low = df['Low'].iloc[i]
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            avg_size = df['Avg_Order_Size'].iloc[i]
            avg_size_ma = df['Avg_Size_MA20'].iloc[i]
            sma50 = df['SMA50'].iloc[i]
            daily_ret = df['Daily_Return'].iloc[i]
            high_20 = df['High_20'].iloc[i]
            
            if pd.isna(sma50) or pd.isna(avg_size_ma) or avg_size_ma == 0 or pd.isna(vol_ma) or pd.isna(high_20):
                i += 1
                continue
            
            # Rules with Structure Filter:
            trend_ok = close > sma50
            stealth_buying = avg_size >= (1.3 * avg_size_ma)
            price_is_quiet = -1.0 <= daily_ret <= 3.5
            volume_ok = vol >= (0.8 * vol_ma)
            near_resistance = close >= (0.95 * high_20)
            
            if trend_ok and stealth_buying and price_is_quiet and volume_ok and near_resistance:
                entry = close
                sl = low * 0.98  # 2% below low
                target = entry * 1.07 # 7% target
                
                future = df.iloc[i+1:i+21]
                hit_target = False
                hit_sl = False
                exit_price = entry
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl:
                        hit_sl = True
                        exit_price = sl
                        days_taken = idx
                        break
                    if r['High'] >= target:
                        hit_target = True
                        exit_price = target
                        days_taken = idx
                        break
                
                if not hit_target and not hit_sl:
                    exit_price = future.iloc[-1]['Close']
                    days_taken = 20
                
                pnl_pct = (exit_price - entry) / entry * 100
                
                trades.append({
                    "Stock": symbol,
                    "Date": df['Date'].iloc[i].strftime('%Y-%m-%d'),
                    "Entry": round(entry, 2),
                    "Target_Hit": hit_target,
                    "PnL%": round(pnl_pct, 2),
                    "Days": days_taken
                })
                
                i += max(days_taken, 5)
            else:
                i += 1
        return trades
    except Exception as e:
        print(f"Error backtesting {symbol}: {e}")
        return []

all_trades = []
if len(STOCKS) > 0:
    for sym in STOCKS:
        res = run_backtest(sym)
        if res:
            all_trades.extend(res)
else:
    print("Watchlist is empty or could not be loaded from Google Sheet.")

df_results = pd.DataFrame(all_trades)
if not df_results.empty:
    total_trades = len(df_results)
    wins = df_results['Target_Hit'].sum()
    win_rate = (wins / total_trades) * 100
    avg_pnl = df_results['PnL%'].mean()
    
    print("\n" + "="*50)
    print("   SHEET-DRIVEN BACKTEST PERFORMANCE REPORT   ")
    print("="*50)
    print(f"Total Filtered Signals      : {total_trades}")
    print(f"Successful Breakouts (7%)   : {wins}")
    print(f"Win Rate                    : {win_rate:.2f}%")
    print(f"Average Return per Trade    : {avg_pnl:.2f}%")
    print("="*50)
    print(df_results.to_string(index=False))
else:
    print("No trades triggered from sheet watchlist.")
            
