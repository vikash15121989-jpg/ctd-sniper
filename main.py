import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== PRE-SURGE SIGN COMPARISON: BREAKOUT vs BREAKDOWN ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def compare_pre_surge_signs(symbol):
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
        
        records = []
        n_rows = len(df)
        
        for i in range(15, n_rows - 20):
            pre_10 = df.iloc[i-10:i]
            if len(pre_10) < 10:
                continue
                
            avg_turnover = pre_10['Turnover'].mean()
            if avg_turnover < 20000000: # Liquidity >= 2 Crore
                continue
                
            max_10d_vol = pre_10['Volume'].max()
            max_10d_high = pre_10['High'].max()
            
            current_vol = df['Volume'].iloc[i]
            current_close = df['Close'].iloc[i]
            
            # Condition: Volume > 10D Max Volume BUT Close < 10D High (Delayed Accumulation Day)
            if current_vol > max_10d_vol and current_close < max_10d_high:
                
                subsequent_10 = df.iloc[i+1 : i+11]
                if subsequent_10.empty:
                    continue
                
                # Check whether it gave Breakout or Breakdown first in next 10 days
                outcome = None
                for idx_ahead, f_row in subsequent_10.iterrows():
                    if f_row['Close'] > max_10d_high:
                        outcome = "BREAKOUT"
                        break
                    elif f_row['Low'] < (max_10d_high * 0.97): # Hit stop-loss / breakdown level
                        outcome = "BREAKDOWN"
                        break
                
                if outcome:
                    pre_tightness = round(pre_10['Candle_Range_%'].mean(), 2)
                    pre_max_order_mult = round((pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max(), 2)
                    net_chg_pct = round((pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100, 2)
                    
                    green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    gr_ratio = round(green_vol / red_vol, 2) if red_vol > 0 else 1.0
                    
                    records.append({
                        "Stock": symbol,
                        "Surge_Date": df['Date'].iloc[i].strftime('%Y-%m-%d'),
                        "Result": outcome,
                        "Pre_Tightness_%": pre_tightness,
                        "Pre_Max_Order_Mult": pre_max_order_mult,
                        "Pre_Net_Chg_%": net_chg_pct,
                        "Pre_Green_Red_Ratio": gr_ratio
                    })
            i += 1
        return records
    except Exception as e:
        return []

all_comparisons = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Comparing pre-surge signs for {sym}...")
        res = compare_pre_surge_signs(sym)
        if res:
            all_comparisons.extend(res)

df_comp = pd.DataFrame(all_comparisons)

if not df_comp.empty:
    print("\n" + "="*70)
    print("   PRE-SURGE SIGN COMPARISON REPORT (BREAKOUT vs BREAKDOWN)   ")
    print("="*70)
    
    breakout_df = df_comp[df_comp['Result'] == 'BREAKOUT']
    breakdown_df = df_comp[df_comp['Result'] == 'BREAKDOWN']
    
    print(f"BREAKOUT CASES (Total: {len(breakout_df)}):")
    if not breakout_df.empty:
        print(f" - Avg Pre-Surge Tightness : {round(breakout_df['Pre_Tightness_%'].mean(), 2)}%")
        print(f" - Avg Max Order Multiplier: {round(breakout_df['Pre_Max_Order_Mult'].mean(), 2)}x")
        print(f" - Avg Pre Net Price Change: {round(breakout_df['Pre_Net_Chg_%'].mean(), 2)}%")
        print(f" - Avg Green/Red Vol Ratio : {round(breakout_df['Pre_Green_Red_Ratio'].mean(), 2)}")
    
    print("-"*70)
    
    print(f"BREAKDOWN CASES (Total: {len(breakdown_df)}):")
    if not breakdown_df.empty:
        print(f" - Avg Pre-Surge Tightness : {round(breakdown_df['Pre_Tightness_%'].mean(), 2)}%")
        print(f" - Avg Max Order Multiplier: {round(breakdown_df['Pre_Max_Order_Mult'].mean(), 2)}x")
        print(f" - Avg Pre Net Price Change: {round(breakdown_df['Pre_Net_Chg_%'].mean(), 2)}%")
        print(f" - Avg Green/Red Vol Ratio : {round(breakdown_df['Pre_Green_Red_Ratio'].mean(), 2)}")
    
    print("="*70)

    try:
        ws = sh.worksheet("PRE_SURGE_SIGN_COMPARISON") if "PRE_SURGE_SIGN_COMPARISON" in [x.title for x in sh.worksheets()] else sh.add_worksheet("PRE_SURGE_SIGN_COMPARISON", 2000, 10)
        ws.clear()
        
        ws.append_row([f"PRE-SURGE SIGN COMPARISON STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Surge Date", "Result", "Pre Tightness %", "Pre Max Order Mult", "Pre Net Chg %", "Pre Green/Red Vol Ratio"])
        ws.append_rows(df_comp.values.tolist())
        print("✅ Comparison study exported to Google Sheet tab 'PRE_SURGE_SIGN_COMPARISON'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No comparison data matched.")
    
