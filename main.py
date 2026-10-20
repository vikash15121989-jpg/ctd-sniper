import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== ULTIMATE FILTERED DELAYED BREAKOUT BACKTESTER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def run_ultimate_delayed_backtest(symbol):
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
                
            # Filter 1: Min Turnover >= 2 Cr
            avg_turnover = pre_10['Turnover'].mean()
            if avg_turnover < 20000000:
                continue
                
            max_10d_vol = pre_10['Volume'].max()
            max_10d_high = pre_10['High'].max()
            
            current_vol = df['Volume'].iloc[i]
            current_close = df['Close'].iloc[i]
            
            # Condition: Volume > 10D Max Volume, but Close < 10D High
            if current_vol > max_10d_vol and current_close < max_10d_high:
                
                # Filter 2: Smart Money Accumulation Metrics
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                avg_tightness = pre_10['Candle_Range_%'].mean()
                
                # Filter 3: Pre-Surge Green/Red Volume Ratio & Trend Check
                green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                gr_ratio = green_vol / red_vol if red_vol > 0 else 10.0
                
                pre_net_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                # Applying all High-Conviction Edge Criteria
                if (max_order_mult >= 1.8 and 
                    avg_tightness <= 4.0 and 
                    gr_ratio >= 2.5 and 
                    pre_net_chg > 0):
                    
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
                                    "GR_Ratio": round(gr_ratio, 2),
                                    "Pre_Net_Chg_%": round(pre_net_chg, 2)
                                })
                            break
            i += 1
        return trade_rows
    except Exception as e:
        return []

all_ultimate_trades = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Running ultimate backtest for {sym}...")
        res = run_ultimate_delayed_backtest(sym)
        if res:
            all_ultimate_trades.extend(res)

df_ult = pd.DataFrame(all_ultimate_trades)

if not df_ult.empty:
    tot = len(df_ult)
    wins = len(df_ult[df_ult['Outcome'] == 'WIN'])
    losses = len(df_ult[df_ult['Outcome'] == 'LOSS'])
    win_rate = round((wins / tot) * 100, 2) if tot > 0 else 0
    total_pnl = round((wins * 6.0) - (losses * 3.0), 2)
    
    print("\n" + "="*65)
    print("   ULTIMATE FILTERED DELAYED BREAKOUT REPORT   ")
    print("="*65)
    print(f"Total Qualified Trades         : {tot}")
    print(f"Wins (Target >= 6% Hit)        : {wins} ({win_rate}%)")
    print(f"Losses (Stop Loss Hit)         : {losses}")
    print(f"Cumulative Fixed P&L           : +{total_pnl}% (Avg: +{round(total_pnl/tot,2)}% per trade)")
    print("="*65)

    try:
        ws = sh.worksheet("ULTIMATE_DELAYED_STUDY") if "ULTIMATE_DELAYED_STUDY" in [x.title for x in sh.worksheets()] else sh.add_worksheet("ULTIMATE_DELAYED_STUDY", 2000, 10)
        ws.clear()
        
        ws.append_row([f"ULTIMATE DELAYED BREAKOUT STUDY | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Total Trades", "Wins", "Win Rate %", "Losses", "Total P&L %"])
        ws.append_row([tot, wins, f"{win_rate}%", losses, f"+{total_pnl}%"])
        ws.append_row([])
        
        ws.append_row(["Stock", "Surge Date", "Breakout Date", "Entry Price", "Outcome", "Green/Red Vol Ratio", "Pre Net Chg %"])
        ws.append_rows(df_ult.values.tolist())
        print("✅ Ultimate delayed study exported to Google Sheet tab 'ULTIMATE_DELAYED_STUDY'!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No trades matched the ultimate delayed criteria.")
    
