import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== SURGE CAUSE & CATEGORIZATION ANALYZER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def categorize_surge_causes(symbol):
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
        
        categorized = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 10):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Every Volume Surge Trigger (>= 2.5x of 20MA)
            if vol >= (2.5 * vol_ma):
                surge_date = df['Date'].iloc[i].strftime('%Y-%m-%d')
                surge_price = df['Close'].iloc[i]
                
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # --- CATEGORIZATION LOGIC ---
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    category = "1. Smart Money Accumulation (Quiet Size Spike)"
                elif net_price_chg > 5.0:
                    category = "2. Existing Momentum / Trend Move"
                else:
                    category = "3. Random / News / High Volatility Event"
                
                categorized.append({
                    "Stock": symbol,
                    "Surge_Date": surge_date,
                    "Surge_Price": round(surge_price, 2),
                    "Pre_10D_Max_Order_Mult": round(max_order_mult, 2),
                    "Pre_10D_Net_Chg_%": round(net_price_chg, 2),
                    "Category": category
                })
                i += 5
        return categorized
    except Exception as e:
        return []

all_categorized = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Categorizing surge triggers for {sym}...")
        res = categorize_surge_causes(sym)
        if res:
            all_categorized.extend(res)

df_cat = pd.DataFrame(all_categorized)

if not df_cat.empty:
    total_surges = len(df_cat)
    cat1_count = len(df_cat[df_cat['Category'].str.startswith('1.')])
    cat2_count = len(df_cat[df_cat['Category'].str.startswith('2.')])
    cat3_count = len(df_cat[df_cat['Category'].str.startswith('3.')])
    
    cat1_pct = round((cat1_count / total_surges) * 100, 2)
    cat2_pct = round((cat2_count / total_surges) * 100, 2)
    cat3_pct = round((cat3_count / total_surges) * 100, 2)
    
    print("\n" + "="*65)
    print("   VOLUME SURGE CAUSE & DISTRIBUTION REPORT   ")
    print("="*65)
    print(f"Total Volume Surges Analyzed            : {total_surges}")
    print(f"1. Smart Money Footprint (Accumulation) : {cat1_count} ({cat1_pct}%)")
    print(f"2. Existing Momentum Run                : {cat2_count} ({cat2_pct}%)")
    print(f"3. Random / News / Volatile Event       : {cat3_count} ({cat3_pct}%)")
    print("="*65)

    try:
        ws = sh.worksheet("SURGE_CATEGORIZATION") if "SURGE_CATEGORIZATION" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SURGE_CATEGORIZATION", 2000, 10)
        ws.clear()
        
        ws.append_row([f"SURGE BREAKDOWN DISTRIBUTION | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Surges", "Smart Money Accumulation Count", "Smart Money %", "Momentum Run Count", "Momentum %", "Random/News Count", "Random %"])
        ws.append_row([total_surges, cat1_count, f"{cat1_pct}%", cat2_count, f"{cat2_pct}%", cat3_count, f"{cat3_pct}%"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Surge Date", "Surge Price", "Pre 10D Max Order Mult", "Pre 10D Net Chg %", "Category"])
        ws.append_rows(df_cat.values.tolist())
        print("✅ Categorized distribution exported to Google Sheet tab 'SURGE_CATEGORIZATION'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No surge data categorized.")
    
