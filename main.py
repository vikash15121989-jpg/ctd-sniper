import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== PRE-ENTRY CLUE FINDER (WINNERS VS LOSERS 10-DAY FOOTPRINT) ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def extract_pre_entry_clues(symbol):
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
        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        
        clue_records = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 20):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger
            if vol >= (2.5 * vol_ma):
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Smart Money Consolidation Condition
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    range_high = pre_10['High'].max()
                    
                    for idx_ahead in range(i+1, min(i+8, n_rows - 15)):
                        if df['Close'].iloc[idx_ahead] > range_high:
                            entry_price = range_high
                            stop_loss = entry_price * 0.97
                            target_price = entry_price * 1.06
                            
                            future_15 = df.iloc[idx_ahead+1 : idx_ahead+16]
                            if future_15.empty:
                                break
                                
                            outcome = "EXPIRED"
                            for _, f_row in future_15.iterrows():
                                if f_row['High'] >= target_price:
                                    outcome = "WIN"
                                    break
                                elif f_row['Low'] <= stop_loss:
                                    outcome = "LOSS"
                                    break
                            
                            if outcome in ["WIN", "LOSS"]:
                                # --- EXTRACT PRE-ENTRY CLUES (10 DAYS BEFORE ENTRY) ---
                                min_vol_ratio = round(pre_10['Volume'].min() / pre_10['Vol_MA20'].mean(), 2)
                                avg_tightness = round(pre_10['Candle_Range_%'].mean(), 2)
                                days_above_ema20 = (pre_10['Close'] > pre_10['EMA20']).sum()
                                spike_days_cnt = ((pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']) >= 1.5).sum()
                                
                                # Up day vs Down day vol ratio
                                green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                                red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                                green_red_vol_ratio = round(green_vol / red_vol, 2) if red_vol > 0 else 1.0
                                
                                clue_records.append({
                                    "Stock": symbol,
                                    "Outcome": outcome,
                                    "Pre_Vol_Dry_Ratio": min_vol_ratio,         # Lower is better (dry volume)
                                    "Pre_Candle_Tightness_%": avg_tightness,   # Lower is better (tight consolidation)
                                    "Days_Above_EMA20": days_above_ema20,      # Higher is better (holding EMA20 support)
                                    "Order_Spike_Days_Cnt": spike_days_cnt,    # Number of days with order size spike
                                    "Green_vs_Red_Vol_Ratio": green_red_vol_ratio # Up day volume vs down day volume
                                })
                            break
                    i += 15
        return clue_records
    except Exception as e:
        return []

all_clues = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Extracting pre-entry clues for {sym}...")
        res = extract_pre_entry_clues(sym)
        if res:
            all_clues.extend(res)

df_clues = pd.DataFrame(all_clues)

if not df_clues.empty:
    win_clues = df_clues[df_clues['Outcome'] == 'WIN']
    loss_clues = df_clues[df_clues['Outcome'] == 'LOSS']
    
    print("\n" + "="*65)
    print("   PRE-ENTRY FOOTPRINT CLUES: WINNERS VS LOSERS   ")
    print("="*65)
    print(f"1. Lowest Vol Dry Ratio      | Winners: {round(win_clues['Pre_Vol_Dry_Ratio'].mean(),2)}x  | Losers: {round(loss_clues['Pre_Vol_Dry_Ratio'].mean(),2)}x")
    print(f"2. Daily Candle Tightness % | Winners: {round(win_clues['Pre_Candle_Tightness_%'].mean(),2)}%  | Losers: {round(loss_clues['Pre_Candle_Tightness_%'].mean(),2)}%")
    print(f"3. Days Above 20 EMA (out of 10)| Winners: {round(win_clues['Days_Above_EMA20'].mean(),1)} Days | Losers: {round(loss_clues['Days_Above_EMA20'].mean(),1)} Days")
    print(f"4. Order Spike Days Count   | Winners: {round(win_clues['Order_Spike_Days_Cnt'].mean(),1)} Days | Losers: {round(loss_clues['Order_Spike_Days_Cnt'].mean(),1)} Days")
    print(f"5. Green vs Red Vol Ratio   | Winners: {round(win_clues['Green_vs_Red_Vol_Ratio'].mean(),2)}x  | Losers: {round(loss_clues['Green_vs_Red_Vol_Ratio'].mean(),2)}x")
    print("="*65)

    try:
        ws = sh.worksheet("PRE_ENTRY_CLUES_STUDY") if "PRE_ENTRY_CLUES_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("PRE_ENTRY_CLUES_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"PRE-ENTRY CLUES STUDY (WINNERS VS LOSERS) | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Metric", "Winners Avg", "Losers Avg", "Difference / Actionable Clue"])
        ws.append_row(["Lowest Vol Dry Ratio", round(win_clues['Pre_Vol_Dry_Ratio'].mean(),2), round(loss_clues['Pre_Vol_Dry_Ratio'].mean(),2), "Drier volume before surge"])
        ws.append_row(["Daily Candle Tightness %", round(win_clues['Pre_Candle_Tightness_%'].mean(),2), round(loss_clues['Pre_Candle_Tightness_%'].mean(),2), "Tighter price range"])
        ws.append_row(["Days Above 20 EMA", round(win_clues['Days_Above_EMA20'].mean(),1), round(loss_clues['Days_Above_EMA20'].mean(),1), "Price holding above 20 EMA"])
        ws.append_row(["Order Spike Days Count", round(win_clues['Order_Spike_Days_Cnt'].mean(),1), round(loss_clues['Order_Spike_Days_Cnt'].mean(),1), "Multiple order size spike days"])
        ws.append_row(["Green vs Red Vol Ratio", round(win_clues['Green_vs_Red_Vol_Ratio'].mean(),2), round(loss_clues['Green_vs_Red_Vol_Ratio'].mean(),2), "Buying volume dominance"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Outcome", "Pre Vol Dry Ratio", "Pre Candle Tightness %", "Days Above EMA20", "Order Spike Days Cnt", "Green vs Red Vol Ratio"])
        ws.append_rows(df_clues.values.tolist())
        print("✅ Pre-entry clues study exported to Google Sheet tab 'PRE_ENTRY_CLUES_STUDY'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
        
