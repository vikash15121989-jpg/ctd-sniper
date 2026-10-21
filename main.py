import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== LIVE DELAYED BREAKOUT SETUP SCANNER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

def scan_live_delayed_setups(symbol):
    try:
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=60)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        if df is None or df.empty or len(df) < 25:
            return None
            
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
            return None

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        df['Turnover'] = df['Volume'] * df['Close']
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Candle_Range_%'] = (df['High'] - df['Low']) / df['Low'] * 100
        
        n_rows = len(df)
        if n_rows < 15:
            return None
            
        # Check in the last 10 trading sessions for the volume surge setup
        for i in range(n_rows - 10, n_rows - 1):
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
                
                max_order_mult = (pre_10['Avg_Order_Size'] / pre_10['Avg_Size_MA20']).max()
                avg_tightness = pre_10['Candle_Range_%'].mean()
                
                green_vol = pre_10[pre_10['Close'] >= pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                red_vol = pre_10[pre_10['Close'] < pre_10['Close'].shift(1).fillna(pre_10['Close'])]['Volume'].sum()
                gr_ratio = green_vol / red_vol if red_vol > 0 else 10.0
                
                pre_net_chg = (pre_10['Close'].iloc[-1] - pre_10['Close'].iloc[0]) / pre_10['Close'].iloc[0] * 100
                
                if (max_order_mult >= 1.8 and 
                    avg_tightness <= 4.0 and 
                    gr_ratio >= 2.5 and 
                    pre_net_chg > 0):
                    
                    surge_date = df['Date'].iloc[i].strftime('%Y-%m-%d')
                    entry_p = round(max_10d_high, 2)
                    sl_p = round(entry_p * 0.97, 2)
                    tgt_p = round(entry_p * 1.06, 2)
                    
                    # Hyperlink Formula for Google Sheet
                    chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{symbol}"
                    chart_hyperlink = f'=HYPERLINK("{chart_url}", "Open {symbol} Chart")'
                    
                    return {
                        "Surge_Date": surge_date,
                        "Symbol": symbol,
                        "Entry_P": entry_p,
                        "SL_P": sl_p,
                        "Tgt_P": tgt_p,
                        "Chart": chart_hyperlink
                    }
        return None
    except Exception as e:
        return None

scanned_setups = []
if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Scanning live delayed setup for {sym}...")
        res = scan_live_delayed_setups(sym)
        if res:
            scanned_setups.append(res)

if len(scanned_setups) > 0:
    df_setups = pd.DataFrame(scanned_setups)
    
    # Sort by Recent Date First (Descending)
    df_setups['Surge_Date_dt'] = pd.to_datetime(df_setups['Surge_Date'])
    df_setups = df_setups.sort_values(by='Surge_Date_dt', ascending=False).drop(columns=['Surge_Date_dt'])
    
    formatted_rows = df_setups.values.tolist()

    try:
        ws = sh.worksheet("READY_FOR_TOMORROW") if "READY_FOR_TOMORROW" in [x.title for x in sh.worksheets()] else sh.add_worksheet("READY_FOR_TOMORROW", 2000, 10)
        ws.clear()
        
        ws.append_row([f"LIVE DELAYED BREAKOUT SETUPS | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["A: Surge Date", "B: Share Name", "C: Entry Price", "D: SL Price", "E: Target Price", "F: Chart Link"])
        
        # Insert rows using USER_ENTERED option to parse formulas correctly
        ws.append_rows(formatted_rows, value_input_option='USER_ENTERED')
        print("✅ Sorted Live Setups with TradingView Hyperlinks exported to Google Sheet!")
    except Exception as e:
        print(f"Failed to export to Google Sheet: {e}")
else:
    print("No live delayed setups found matching criteria today.")
    
