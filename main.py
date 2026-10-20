import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== SURGE OUTCOME BACKTESTER (BREAKOUT VS BREAKDOWN) ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def analyze_surge_outcome(symbol):
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
        
        outcomes = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 15):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger (Min 2.5x of 20MA)
            if vol >= (2.5 * vol_ma):
                surge_date = df['Date'].iloc[i].strftime('%Y-%m-%d')
                surge_price = df['Close'].iloc[i]
                
                # Check preceding 10 days condition (Smart money footprint check)
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Filter: If pre-surge had order size spike >= 1.8x and net price was relatively flat (-2% to +3%)
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    
                    # Track post-surge performance for next 10 days
                    post_10 = df.iloc[i+1:i+11]
                    if post_10.empty:
                        continue
                        
                    max_high = post_10['High'].max()
                    min_low = post_10['Low'].min()
                    
                    up_pct = round((max_high - surge_price) / surge_price * 100, 2)
                    down_pct = round((min_low - surge_price) / surge_price * 100, 2)
                    
                    if up_pct >= 6.0 and abs(down_pct) < 3.0:
                        result = "Strong Breakout (UP)"
                    elif down_pct <= -3.0 and up_pct < 5.0:
                        result = "Breakdown (DOWN)"
                    else:
                        result = "Sideways / Mixed"
                        
                    outcomes.append({
                        "Stock": symbol,
                        "Surge_Date": surge_date,
                        "Surge_Price": round(surge_price, 2),
                        "Pre_Surge_Max_Order_Mult": round(max_order_mult, 2),
                        "Pre_10D_Net_Chg_%": round(net_price_chg, 2),
                        "Post_Surge_Max_Up_%": up_pct,
                        "Post_Surge_Max_Down_%": down_pct,
                        "Outcome_Result": result
                    })
                    
                    i += 10 # skip to next window
        return outcomes
    except Exception as e:
        return []

all_outcomes = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Analyzing surge outcomes for {sym}...")
        res = analyze_surge_outcome(sym)
        if res:
            all_outcomes.extend(res)

df_out = pd.DataFrame(all_outcomes)

if not df_out.empty:
    print("\n" + "="*60)
    print("   SURGE OUTCOME PERFORMANCE REPORT   ")
    print("="*60)
    print(f"Total Filtered Surges Analyzed        : {len(df_out)}")
    print(f"Strong Breakouts (UP)                 : {len(df_out[df_out['Outcome_Result'] == 'Strong Breakout (UP)'])}")
    print(f"Breakdowns (DOWN)                     : {len(df_out[df_out['Outcome_Result'] == 'Breakdown (DOWN)'])}")
    print("="*60)

    try:
        ws = sh.worksheet("SURGE_OUTCOME_STUDY") if "SURGE_OUTCOME_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SURGE_OUTCOME_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"SURGE OUTCOME STUDY (BREAKOUT VS BREAKDOWN) | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Surge Date", "Surge Price", "Pre Surge Max Order Mult", "Pre 10D Net Chg %", "Post Surge Max Up %", "Post Surge Max Down %", "Outcome Result"])
        ws.append_rows(df_out.values.tolist())
        print("✅ Surge outcome study exported to Google Sheet tab 'SURGE_OUTCOME_STUDY'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No matching surge outcomes found.")
    
