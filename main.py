import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== MULTI-FILTER PRE-SURGE COMBINATION BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def run_multi_filter_backtest(symbol):
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
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        
        filtered_trades = []
        n_rows = len(df)
        
        for i in range(25, n_rows - 20):
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
                
            # Volume Surge Trigger (Min 2.5x of 20MA)
            if vol >= (2.5 * vol_ma):
                pre_10 = df.iloc[i-10:i]
                if len(pre_10) < 10:
                    continue
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                net_price_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Base Smart Money Consolidation Condition
                if max_order_mult >= 1.8 and -2.0 <= net_price_chg <= 3.0:
                    
                    # --- COMBINED MULTI-FILTERS ---
                    avg_tightness = pre_10['Candle_Range_%'].mean()
                    spike_days_cnt = ((pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']) >= 1.5).sum()
                    
                    green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                    green_red_ratio = green_vol / red_vol if red_vol > 0 else 1.0
                    
                    # Apply Checklist Filters
                    if avg_tightness <= 4.0 and spike_days_cnt >= 2 and green_red_ratio >= 2.5:
                        
                        range_high = pre_10['High'].max()
                        
                        # Look ahead for Method 5 Entry (Breakout above range high)
                        for idx_ahead in range(i+1, min(i+8, n_rows - 15)):
                            if df['Close'].iloc[idx_ahead] > range_high:
                                entry_date = df['Date'].iloc[idx_ahead].strftime('%Y-%m-%d')
                                entry_price = range_high
                                stop_loss = entry_price * 0.97 # 3% SL
                                target_price = entry_price * 1.06 # 6% Target
                                
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
                                    filtered_trades.append({
                                        "Stock": symbol,
                                        "Entry_Date": entry_date,
                                        "Entry_Price": round(entry_price, 2),
                                        "Outcome": outcome,
                                        "Tightness_%": round(avg_tightness, 2),
                                        "Spike_Days": spike_days_cnt,
                                        "Green_Red_Vol_Ratio": round(green_red_ratio, 2)
                                    })
                                break
                        i += 15
        return filtered_trades
    except Exception as e:
        return []

all_filtered = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Running combined filter backtest for {sym}...")
        res = run_multi_filter_backtest(sym)
        if res:
            all_filtered.extend(res)

df_fil = pd.DataFrame(all_filtered)

if not df_fil.empty:
    tot = len(df_fil)
    wins = len(df_fil[df_fil['Outcome'] == 'WIN'])
    losses = len(df_fil[df_fil['Outcome'] == 'LOSS'])
    win_rate = round((wins / tot) * 100, 2) if tot > 0 else 0
    
    print("\n" + "="*65)
    print("   MULTI-FILTER COMBINATION PERFORMANCE REPORT   ")
    print("="*65)
    print(f"Total Filtered High-Quality Trades    : {tot}")
    print(f"Target Hit (>= 6% Gain) Wins          : {wins} ({win_rate}%)")
    print(f"Stop Loss Hit Losses                  : {losses}")
    print("="*65)

    try:
        ws = sh.worksheet("MULTI_FILTER_STUDY") if "MULTI_FILTER_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("MULTI_FILTER_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"MULTI-FILTER COMBINATION STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Trades", "Wins (>=6%)", "Win Rate %", "Losses (SL Hit)"])
        ws.append_row([tot, wins, f"{win_rate}%", losses])
        ws.append_row([])
        
        ws.append_row(["Stock", "Entry Date", "Entry Price", "Outcome", "Tightness %", "Spike Days", "Green Red Vol Ratio"])
        ws.append_rows(df_fil.values.tolist())
        print("✅ Multi-filter report exported to Google Sheet tab 'MULTI_FILTER_STUDY'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No trades matched the combined multi-filter checklist.")
    
