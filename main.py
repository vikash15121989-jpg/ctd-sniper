import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== FILTERED DELAYED BREAKOUT BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def run_filtered_delayed_backtest(symbol):
    try:
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=365)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 60:
            return []
            
        df.columns = [str(c).strip().title() for c in df.columns]
        df = df.loc[:, ~df.columns.duplicated()]
        
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
        
        df['Turnover'] = df['Volume'] * df['Close']
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        
        trade_rows = []
        n_rows = len(df)
        
        for i in range(15, n_rows - 20):
            pre_10 = df.iloc[i-10:i]
            if len(pre_10) < 10:
                continue
                
            # Liquidity Filter Check (Min 2 Crore Turnover)
            avg_turnover = pre_10['Turnover'].mean()
            if avg_turnover < 20000000:
                continue
                
            max_10d_vol = pre_10['Volume'].max()
            max_10d_high = pre_10['High'].max()
            
            current_vol = df['Volume'].iloc[i]
            current_high = df['High'].iloc[i]
            current_close = df['Close'].iloc[i]
            
            # Volume surge greater than last 10 days max volume, but close did NOT break the 10-day high
            if current_vol > max_10d_vol and current_close < max_10d_high:
                
                # Smart Money Footprint Check on pre-10 days accumulation
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                avg_tightness = pre_10['Candle_Range_%'].mean()
                
                if max_order_mult >= 1.8 and avg_tightness <= 4.0:
                    
                    # Check if it breaks the 10-day high within the next 10 days
                    subsequent_10 = df.iloc[i+1 : i+11]
                    if subsequent_10.empty:
                        continue
                    
                    for idx_ahead, f_row in subsequent_10.iterrows():
                        if f_row['Close'] > max_10d_high:
                            entry_date = f_row['Date'].strftime('%Y-%m-%d')
                            entry_p = max_10d_high
                            
                            future_15 = df.iloc[idx_ahead+1 : idx_ahead+16]
                            if future_15.empty:
                                break
                                
                            sl = entry_p * 0.97
                            tgt = entry_p * 1.06
                            outcome = "EXPIRED"
                            
                            for _, fut in future_15.iterrows():
                                if fut['High'] >= tgt:
                                    outcome = "WIN"
                                    break
                                elif fut['Low'] <= sl:
                                    outcome = "LOSS"
                                    break
                            
                            if outcome in ["WIN", "LOSS"]:
                                trade_rows.append({
                                    "Stock": symbol,
                                    "Surge_Date": df['Date'].iloc[i].strftime('%Y-%m-%d'),
                                    "Breakout_Date": entry_date,
                                    "Entry_Price": round(entry_p, 2),
                                    "Outcome": outcome,
                                    "Turnover_Cr": round(avg_turnover / 10000000, 2)
                                })
                            break
            i += 1
        return trade_rows
    except Exception as e:
        return []

all_filtered_delayed = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Running filtered delayed breakout backtest for {sym}...")
        res = run_filtered_delayed_backtest(sym)
        if res:
            all_filtered_delayed.extend(res)

df_fdel = pd.DataFrame(all_filtered_delayed)

if not df_fdel.empty:
    tot = len(df_fdel)
    wins = len(df_fdel[df_fdel['Outcome'] == 'WIN'])
    losses = len(df_fdel[df_fdel['Outcome'] == 'LOSS'])
    win_rate = round((wins / tot) * 100, 2) if tot > 0 else 0
    total_pnl = round((wins * 6.0) - (losses * 3.0), 2)
    
    print("\n" + "="*65)
    print("   FILTERED DELAYED BREAKOUT PERFORMANCE REPORT   ")
    print("="*65)
    print(f"Total Qualified Filtered Trades : {tot}")
    print(f"Wins (Target >= 6% Hit)         : {wins} ({win_rate}%)")
    print(f"Losses (Stop Loss Hit)          : {losses}")
    print(f"Cumulative Fixed P&L            : +{total_pnl}% (Avg: +{round(total_pnl/tot,2)}% per trade)")
    print("="*65)

    try:
        ws = sh.worksheet("FILTERED_DELAYED_STUDY") if "FILTERED_DELAYED_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("FILTERED_DELAYED_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"FILTERED DELAYED BREAKOUT STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Trades", "Wins", "Win Rate %", "Losses", "Total P&L %"])
        ws.append_row([tot, wins, f"{win_rate}%", losses, f"+{total_pnl}%"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Surge Date", "Breakout Date", "Entry Price", "Outcome", "Turnover (Cr)"])
        ws.append_rows(df_fdel.values.tolist())
        print("✅ Filtered delayed breakout study exported to Google Sheet tab 'FILTERED_DELAYED_STUDY'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No trades matched the filtered delayed breakout criteria.")
    
