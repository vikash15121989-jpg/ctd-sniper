import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== METHOD 5 (10D RANGE HIGH BREAKOUT) DEEP DIVE ANALYZER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def analyze_method_5_details(symbol):
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
            elif 'open' in clow:
                col_map[c] = 'Open'
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
        
        m5_records = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 20):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger (Min 2.5x of 20MA)
            if vol >= (2.5 * vol_ma):
                surge_vol_mult = round(vol / vol_ma, 2)
                
                # Check preceding 10 days Smart Money Footprint
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Smart Money Condition
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    
                    # METHOD 5 LOGIC: 10-Day Consolidation High Breakout
                    range_high = pre_10['High'].max()
                    
                    # Look ahead for breakout entry
                    for idx_ahead in range(i+1, min(i+8, n_rows - 15)):
                        if df['Close'].iloc[idx_ahead] > range_high:
                            entry_date = df['Date'].iloc[idx_ahead].strftime('%Y-%m-%d')
                            entry_price = range_high
                            stop_loss = entry_price * 0.97 # 3% SL
                            target_price = entry_price * 1.06 # 6% Target
                            
                            # Track performance post entry
                            future_15 = df.iloc[idx_ahead+1 : idx_ahead+16]
                            if future_15.empty:
                                break
                                
                            outcome = "EXPIRED"
                            days_to_result = 0
                            max_up_pct = 0.0
                            
                            for d_count, (_, f_row) in enumerate(future_15.iterrows(), 1):
                                f_high = f_row['High']
                                f_low = f_row['Low']
                                
                                up_move = (f_high - entry_price) / entry_price * 100
                                if up_move > max_up_pct:
                                    max_up_pct = round(up_move, 2)
                                    
                                if f_high >= target_price:
                                    outcome = "PROFIT (>=6%)"
                                    days_to_result = d_count
                                    break
                                elif f_low <= stop_loss:
                                    outcome = "LOSS (SL Hit)"
                                    days_to_result = d_count
                                    break
                            
                            m5_records.append({
                                "Stock": symbol,
                                "Entry_Date": entry_date,
                                "Entry_Price": round(entry_price, 2),
                                "Surge_Vol_Mult": surge_vol_mult,
                                "Pre_10D_Order_Mult": round(max_order_mult, 2),
                                "Outcome": outcome,
                                "Days_Taken": days_to_result if days_to_result > 0 else "-",
                                "Max_Gain_%": max_up_pct
                            })
                            break
                    i += 15
        return m5_records
    except Exception as e:
        return []

all_m5 = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Analyzing Method 5 trades for {sym}...")
        res = analyze_method_5_details(sym)
        if res:
            all_m5.extend(res)

df_m5 = pd.DataFrame(all_m5)

if not df_m5.empty:
    total_trades = len(df_m5)
    profits = df_m5[df_m5['Outcome'] == 'PROFIT (>=6%)']
    losses = df_m5[df_m5['Outcome'] == 'LOSS (SL Hit)']
    
    win_cnt = len(profits)
    loss_cnt = len(losses)
    win_pct = round((win_cnt / total_trades) * 100, 2)
    
    avg_win_days = round(pd.to_numeric(profits['Days_Taken'], errors='coerce').mean(), 1)
    avg_max_gain = round(profits['Max_Gain_%'].mean(), 2)
    avg_surge_vol_win = round(profits['Surge_Vol_Mult'].mean(), 2)
    avg_surge_vol_loss = round(losses['Surge_Vol_Mult'].mean(), 2)
    
    print("\n" + "="*65)
    print("   METHOD 5 (10D RANGE HIGH BREAKOUT) DEEP ANALYSIS REPORT   ")
    print("="*65)
    print(f"Total Method 5 Trades                  : {total_trades}")
    print(f"Profit Trades (Target >= 6% Hit)       : {win_cnt} ({win_pct}%)")
    print(f"Loss Trades (Stop Loss Hit)            : {loss_cnt}")
    print(f"Average Days to Hit 6% Profit          : {avg_win_days} Days")
    print(f"Average Max Gain in Winning Trades     : +{avg_max_gain}%")
    print(f"Average Surge Vol Multiple in WIN Trades: {avg_surge_vol_win}x")
    print(f"Average Surge Vol Multiple in LOSS Trades: {avg_surge_vol_loss}x")
    print("="*65)

    try:
        ws = sh.worksheet("METHOD_5_DEEP_DIVE") if "METHOD_5_DEEP_DIVE" in [x.title for x in sh.worksheets()] else sh.add_worksheet("METHOD_5_DEEP_DIVE", 2000, 10)
        ws.clear()
        
        ws.append_row([f"METHOD 5 DEEP ANALYSIS | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Trades", "Profits (>=6%)", "Win Rate %", "Losses (SL Hit)", "Avg Days to Profit", "Avg Max Gain %", "Avg Vol Mult (Win)", "Avg Vol Mult (Loss)"])
        ws.append_row([total_trades, win_cnt, f"{win_pct}%", loss_cnt, avg_win_days, f"{avg_max_gain}%", f"{avg_surge_vol_win}x", f"{avg_surge_vol_loss}x"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Entry Date", "Entry Price", "Surge Vol Mult", "Pre 10D Order Mult", "Outcome", "Days Taken", "Max Gain %"])
        ws.append_rows(df_m5.values.tolist())
        print("✅ Deep analysis exported to Google Sheet tab 'METHOD_5_DEEP_DIVE'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No Method 5 trades found.")
    
