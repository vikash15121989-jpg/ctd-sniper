import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== 10-METHOD ENTRY OPTIMIZATION BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def run_10_methods_backtest(symbol):
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
        
        # If Open is missing, estimate with Close
        if 'Open' not in df.columns:
            df['Open'] = df['Close'].shift(1).fillna(df['Close'])

        required_cols = ['Volume', 'Trades', 'Close', 'High', 'Low', 'Open', 'Date']
        if not all(col in df.columns for col in required_cols):
            return []

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low', 'Open'])
        
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        
        # Helper function to test a specific entry point and price
        def evaluate_trade(entry_idx, entry_price):
            if entry_price <= 0 or entry_idx >= len(df) - 15:
                return None
                
            stop_loss = entry_price * 0.97 # 3% SL
            target = entry_price * 1.06  # 6% Target
            
            future = df.iloc[entry_idx + 1 : entry_idx + 16]
            if future.empty:
                return None
                
            for _, f_row in future.iterrows():
                if f_row['High'] >= target:
                    return "WIN"
                if f_row['Low'] <= stop_loss:
                    return "LOSS"
            return "EXPIRED"

        method_stats = {m: {"Wins": 0, "Losses": 0, "Total": 0} for m in range(1, 11)}
        n_rows = len(df)
        
        for i in range(25, n_rows - 20):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger (Min 2.5x of 20MA)
            if vol >= (2.5 * vol_ma):
                # Check preceding 10 days Smart Money Footprint
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Smart Money Condition: Order size >= 1.8x & Price flat (-2% to +3%)
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    
                    # --- TEST 10 ENTRY METHODS FOR THIS SURGE ---
                    
                    # Method 1: Surge Day Close
                    res = evaluate_trade(i, df['Close'].iloc[i])
                    if res: 
                        method_stats[1]["Total"] += 1
                        if res == "WIN": method_stats[1]["Wins"] += 1
                        elif res == "LOSS": method_stats[1]["Losses"] += 1

                    # Method 2: Next Day Open
                    if i + 1 < n_rows:
                        res = evaluate_trade(i+1, df['Open'].iloc[i+1])
                        if res:
                            method_stats[2]["Total"] += 1
                            if res == "WIN": method_stats[2]["Wins"] += 1
                            elif res == "LOSS": method_stats[2]["Losses"] += 1

                    # Method 3: Break of Surge High (Look ahead next 5 days for breakout above surge high)
                    surge_high = df['High'].iloc[i]
                    m3_entered = False
                    for idx_ahead in range(i+1, min(i+6, n_rows)):
                        if df['High'].iloc[idx_ahead] >= surge_high:
                            res = evaluate_trade(idx_ahead, surge_high)
                            if res:
                                method_stats[3]["Total"] += 1
                                if res == "WIN": method_stats[3]["Wins"] += 1
                                elif res == "LOSS": method_stats[3]["Losses"] += 1
                            m3_entered = True
                            break

                    # Method 4: Pullback to Surge Low
                    surge_low = df['Low'].iloc[i]
                    for idx_ahead in range(i+1, min(i+8, n_rows)):
                        if df['Low'].iloc[idx_ahead] <= surge_low * 1.01:
                            res = evaluate_trade(idx_ahead, surge_low)
                            if res:
                                method_stats[4]["Total"] += 1
                                if res == "WIN": method_stats[4]["Wins"] += 1
                                elif res == "LOSS": method_stats[4]["Losses"] += 1
                            break

                    # Method 5: 10-Day Consolidation Range High Breakout
                    range_high = pre_10['High'].max()
                    for idx_ahead in range(i+1, min(i+8, n_rows)):
                        if df['Close'].iloc[idx_ahead] > range_high:
                            res = evaluate_trade(idx_ahead, range_high)
                            if res:
                                method_stats[5]["Total"] += 1
                                if res == "WIN": method_stats[5]["Wins"] += 1
                                elif res == "LOSS": method_stats[5]["Losses"] += 1
                            break

                    # Method 6: VWAP/Mean Reversion entry (Using Typical Price as proxy)
                    typical_price = (df['High'].iloc[i] + df['Low'].iloc[i] + df['Close'].iloc[i]) / 3
                    res = evaluate_trade(i, typical_price)
                    if res:
                        method_stats[6]["Total"] += 1
                        if res == "WIN": method_stats[6]["Wins"] += 1
                        elif res == "LOSS": method_stats[6]["Losses"] += 1

                    # Method 7: Pivot Point Resistance Breakout (PP = (H+L+C)/3, R1 = 2*PP - L)
                    pp = (df['High'].iloc[i] + df['Low'].iloc[i] + df['Close'].iloc[i]) / 3
                    r1 = (2 * pp) - df['Low'].iloc[i]
                    for idx_ahead in range(i+1, min(i+6, n_rows)):
                        if df['Close'].iloc[idx_ahead] >= r1:
                            res = evaluate_trade(idx_ahead, r1)
                            if res:
                                method_stats[7]["Total"] += 1
                                if res == "WIN": method_stats[7]["Wins"] += 1
                                elif res == "LOSS": method_stats[7]["Losses"] += 1
                            break

                    # Method 8: 3rd Day After Surge
                    if i + 3 < n_rows:
                        res = evaluate_trade(i+3, df['Close'].iloc[i+3])
                        if res:
                            method_stats[8]["Total"] += 1
                            if res == "WIN": method_stats[8]["Wins"] += 1
                            elif res == "LOSS": method_stats[8]["Losses"] += 1

                    # Method 9: Moving Average confirmation (Close > 5 EMA)
                    ema5 = df['Close'].ewm(span=5).mean()
                    if i < len(ema5) and df['Close'].iloc[i] > ema5.iloc[i]:
                        res = evaluate_trade(i, df['Close'].iloc[i])
                        if res:
                            method_stats[9]["Total"] += 1
                            if res == "WIN": method_stats[9]["Wins"] += 1
                            elif res == "LOSS": method_stats[9]["Losses"] += 1

                    # Method 10: Pre-Surge Last Day Close (Right before volume spike)
                    res = evaluate_trade(i-1, df['Close'].iloc[i-1])
                    if res:
                        method_stats[10]["Total"] += 1
                        if res == "WIN": method_stats[10]["Wins"] += 1
                        elif res == "LOSS": method_stats[10]["Losses"] += 1
                        
                    i += 15 # jump ahead
        return method_stats
    except Exception as e:
        return {}

aggregate_stats = {m: {"Wins": 0, "Losses": 0, "Total": 0} for m in range(1, 11)}

if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Optimizing entry methods for {sym}...")
        res = run_10_methods_backtest(sym)
        if res:
            for m in res:
                aggregate_stats[m]["Wins"] += res[m]["Wins"]
                aggregate_stats[m]["Losses"] += res[m]["Losses"]
                aggregate_stats[m]["Total"] += res[m]["Total"]

summary_rows = []
print("\n" + "="*65)
print("   10-METHOD ENTRY STRATEGY OPTIMIZATION REPORT   ")
print("="*65)

method_names = {
    1: "Method 1: Surge Day Close Entry",
    2: "Method 2: Next Day Open Entry",
    3: "Method 3: Break of Surge High Entry",
    4: "Method 4: Pullback to Surge Low Entry",
    5: "Method 5: 10D Range High Breakout",
    6: "Method 6: Typical Price / VWAP Proxy",
    7: "Method 7: Pivot Point R1 Breakout",
    8: "Method 8: 3rd Day After Surge Entry",
    9: "Method 9: 5 EMA Trend Confirmation",
    10: "Method 10: Pre-Surge Last Day Entry"
}

for m in range(1, 11):
    tot = aggregate_stats[m]["Total"]
    wins = aggregate_stats[m]["Wins"]
    losses = aggregate_stats[m]["Losses"]
    win_rate = round((wins / tot) * 100, 2) if tot > 0 else 0
    
    print(f"{method_names[m]:<38} | Trades: {tot:<5} | Wins: {wins:<5} | Win Rate: {win_rate}%")
    summary_rows.append([method_names[m], tot, wins, losses, f"{win_rate}%"])

print("="*65)

# Export to Google Sheet
try:
    ws = sh.worksheet("ENTRY_OPTIMIZATION_STUDY") if "ENTRY_OPTIMIZATION_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("ENTRY_OPTIMIZATION_STUDY", 2000, 10)
    ws.clear()
    
    ws.append_row([f"10-METHOD ENTRY STRATEGY OPTIMIZATION STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
    ws.append_row(["Entry Method Name", "Total Trades", "Wins (>=6%)", "Losses (SL Hit)", "Win Rate %"])
    ws.append_rows(summary_rows)
    print("✅ Optimization study exported to Google Sheet tab 'ENTRY_OPTIMIZATION_STUDY'!")
except Exception as e:
    print(f"Failed to export to Google Sheet: {e}")
                            
