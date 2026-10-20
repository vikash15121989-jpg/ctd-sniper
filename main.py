import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== SMART MONEY FLOW & POST-SURGE ANALYTICAL BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def analyze_smart_money(symbol):
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
        
        # Calculations
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['Prev_Close'] = df['Close'].shift(1)
        df['Daily_Return'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close'] * 100
        
        records = []
        i = 20
        n_rows = len(df)
        
        while i < n_rows - 15:
            close = df['Close'].iloc[i]
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            avg_size = df['Avg_Order_Size'].iloc[i]
            avg_size_ma = df['Avg_Size_MA20'].iloc[i]
            daily_ret = df['Daily_Return'].iloc[i]
            
            if pd.isna(avg_size_ma) or avg_size_ma == 0 or pd.isna(vol_ma):
                i += 1
                continue
            
            # --- SMART MONEY ACCUMULATION CONDITION ---
            # 1. Order Size Spike (>= 1.5x of 20MA)
            # 2. Quiet Price Movement (-1.5% to +2.0%)
            is_stealth_buy = (avg_size >= 1.5 * avg_size_ma) and (-1.5 <= daily_ret <= 2.0)
            
            if is_stealth_buy:
                signal_date = df['Date'].iloc[i].strftime('%Y-%m-%d')
                signal_price = close
                
                # Look ahead for Volume Surge (Within next 10 trading days)
                future = df.iloc[i+1:i+11]
                surge_found = False
                days_to_surge = 0
                outcome = "No Surge"
                max_move_pct = 0.0
                
                for idx, (_, f_row) in enumerate(future.iterrows(), 1):
                    # Volume Surge defined as >= 2.5x of 20MA Volume
                    if f_row['Volume'] >= (2.5 * f_row['Vol_MA20']):
                        surge_found = True
                        days_to_surge = idx
                        
                        # Check price reaction after surge (10 days after surge)
                        post_surge = df.iloc[i+1+idx : i+11+idx]
                        if not post_surge.empty:
                            max_high = post_surge['High'].max()
                            min_low = post_surge['Low'].min()
                            
                            up_move = (max_high - signal_price) / signal_price * 100
                            down_move = (min_low - signal_price) / signal_price * 100
                            
                            if up_move > abs(down_move):
                                outcome = "Breakout (UP)"
                                max_move_pct = round(up_move, 2)
                            else:
                                outcome = "Breakdown (DOWN)"
                                max_move_pct = round(down_move, 2)
                        break
                
                records.append({
                    "Stock": symbol,
                    "Accumulation_Date": signal_date,
                    "Price_At_Signal": round(signal_price, 2),
                    "Order_Size_Multiple": round(avg_size / avg_size_ma, 2),
                    "Volume_Surge_Happened": "YES" if surge_found else "NO",
                    "Days_To_Surge": days_to_surge if surge_found else "-",
                    "Post_Surge_Direction": outcome,
                    "Max_Move_%": max_move_pct
                })
                
                i += 10 # Jump ahead to avoid tracking duplicate contiguous signals
            else:
                i += 1
                
        return records
    except Exception as e:
        return []

all_study_data = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Analyzing Smart Money Flow for {sym}...")
        res = analyze_smart_money(sym)
        if res:
            all_study_data.extend(res)

df_study = pd.DataFrame(all_study_data)

if not df_study.empty:
    print("\n" + "="*60)
    print("   SMART MONEY & VOLUME SURGE ANALYSIS REPORT   ")
    print("="*60)
    print(f"Total Smart Money Footprints Detected : {len(df_study)}")
    
    surges = df_study[df_study['Volume_Surge_Happened'] == 'YES']
    print(f"Cases Where Volume Surge Followed     : {len(surges)}")
    
    if not surges.empty:
        up_cases = len(surges[surges['Post_Surge_Direction'] == 'Breakout (UP)'])
        down_cases = len(surges[surges['Post_Surge_Direction'] == 'Breakdown (DOWN)'])
        print(f"  └─ Lead to Upward Breakouts        : {up_cases}")
        print(f"  └─ Lead to Downward Breakdowns     : {down_cases}")
    print("="*60)

    # Export Study to Google Sheet
    try:
        ws = sh.worksheet("SMART_MONEY_STUDY") if "SMART_MONEY_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SMART_MONEY_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"SMART MONEY ACCUMULATION & SURGE BEHAVIOR STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Accumulation Date", "Price At Signal", "Order Size Multiple", "Volume Surge Happened", "Days To Surge", "Post Surge Direction", "Max Move %"])
        ws.append_rows(df_study.values.tolist())
        print("✅ Analysis exported to Google Sheet tab 'SMART_MONEY_STUDY'!")
    except Exception as e:
        print(f"Failed to export study to Google Sheet: {e}")
else:
    print("No Smart Money footprints detected.")
    
