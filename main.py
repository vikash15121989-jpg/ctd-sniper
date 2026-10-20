import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== 10-DAYS PRE-SURGE FOOTPRINT ANALYZER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def analyze_10_days_before_surge(symbol):
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
        
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['Prev_Close'] = df['Close'].shift(1)
        df['Daily_Return'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close'] * 100
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        
        analysis_records = []
        n_rows = len(df)
        
        # Scan for Volume Surge Days (Min 2.5x Vol_MA20)
        for i in range(25, n_rows):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            if vol >= (2.5 * vol_ma):
                surge_date = df['Date'].iloc[i].strftime('%Y-%m-%d')
                surge_vol_multiple = round(vol / vol_ma, 2)
                
                # --- EXTRACT EXACT 10 DAYS BEFORE SURGE ---
                pre_10_days = df.iloc[i-10:i]
                
                if len(pre_10_days) < 10:
                    continue
                
                # 1. 10 Dino ka Average Volume vs Vol_MA20 (Volume Squeeze check)
                avg_vol_10d = pre_10_days['Volume'].mean()
                vol_ratio_10d = round(avg_vol_10d / pre_10_days['Vol_MA20'].mean(), 2) if pre_10_days['Vol_MA20'].mean() > 0 else 0
                
                # 2. 10 Dino me Max Order Size Multiple (Smart Money block trades check)
                max_order_mult_10d = round((pre_10_days['Avg_Order_Size'] / pre_10_days['Avg_Size_MA20']).max(), 2)
                
                # 3. 10 Dino ki Average Candle Range % (Price tightness check)
                avg_range_10d = round(pre_10_days['Candle_Range_%'].mean(), 2)
                
                # 4. 10 Dino ka Net Price Change (%)
                price_start = pre_10_days['Close'].iloc[0]
                price_end = pre_10_days['Close'].iloc[-1]
                net_change_10d = round((price_end - price_start) / price_start * 100, 2)
                
                analysis_records.append({
                    "Stock": symbol,
                    "Surge_Date": surge_date,
                    "Surge_Vol_Multiple": surge_vol_multiple,
                    "10D_Avg_Vol_Ratio": vol_ratio_10d,          # 10 din me volume kitna dab/sukha tha
                    "10D_Max_Order_Size_Mult": max_order_mult_10d, # 10 din me sabse bada order size spike
                    "10D_Avg_Candle_Range_%": avg_range_10d,      # 10 din ki range tightness
                    "10D_Net_Price_Change_%": net_change_10d      # 10 din me price kitna chala (sideways tha ya trend me)
                })
                
                # Skip to avoid overlapping same surge window
                i += 10
        return analysis_records
    except Exception as e:
        return []

all_10d_data = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Analyzing 10 days prior to surge for {sym}...")
        res = analyze_10_days_before_surge(sym)
        if res:
            all_10d_data.extend(res)

df_10d = pd.DataFrame(all_10d_data)

if not df_10d.empty:
    print("\n" + "="*60)
    print("   10-DAYS PRE-SURGE FOOTPRINT REPORT   ")
    print("="*60)
    print(f"Total Surges Analyzed                 : {len(df_10d)}")
    print(f"Avg 10D Volume Ratio (Squeeze check)  : {round(df_10d['10D_Avg_Vol_Ratio'].mean(), 2)}x")
    print(f"Avg 10D Max Order Size Multiple       : {round(df_10d['10D_Max_Order_Size_Mult'].mean(), 2)}x")
    print(f"Avg 10D Candle Range %                : {round(df_10d['10D_Avg_Candle_Range_%'].mean(), 2)}%")
    print(f"Avg 10D Net Price Change %            : {round(df_10d['10D_Net_Price_Change_%'].mean(), 2)}%")
    print("="*60)

    # Export to Google Sheet
    try:
        ws = sh.worksheet("TEN_DAYS_BEFORE_SURGE") if "TEN_DAYS_BEFORE_SURGE" in [x.title for x in sh.worksheets()] else sh.add_worksheet("TEN_DAYS_BEFORE_SURGE", 2000, 10)
        ws.clear()
        
        ws.append_row([f"10-DAYS PRE-VOLUME SURGE ANALYSIS | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Surge Date", "Surge Vol Multiple", "10D Avg Vol Ratio", "10D Max Order Size Mult", "10D Avg Candle Range %", "10D Net Price Change %"])
        ws.append_rows(df_10d.values.tolist())
        print("✅ 10-day pre-surge analysis exported to Google Sheet tab 'TEN_DAYS_BEFORE_SURGE'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No volume surges found for 10-day analysis.")
    
