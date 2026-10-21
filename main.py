import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print("=== DUAL-PIPELINE DELAYED BREAKOUT SCANNER ===")

try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]]
    print(f"Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = []

pre_surge_radar = []
active_delayed_setups = []

def process_stock_pipelines(symbol):
    try:
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=60)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        if df is None or df.empty or len(df) < 25:
            return
            
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
            return

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
        if n_rows < 20:
            return

        # -------------------------------------------------------------
        # PIPELINE 1: PRE-SURGE RADAR (Pattern ready, but Volume Surge pending)
        # -------------------------------------------------------------
        last_10 = df.iloc[-10:]
        avg_turnover_p1 = last_10['Turnover'].mean()
        
        if avg_turnover_p1 >= 20000000: # Turnover >= 2 Cr
            max_order_mult_p1 = (last_10['Avg_Order_Size'] / last_10['Avg_Size_MA20']).max()
            avg_tightness_p1 = last_10['Candle_Range_%'].mean()
            
            green_vol_p1 = last_10[last_10['Close'] >= last_10['Close'].shift(1).fillna(last_10['Close'])]['Volume'].sum()
            red_vol_p1 = last_10[last_10['Close'] < last_10['Close'].shift(1).fillna(last_10['Close'])]['Volume'].sum()
            gr_ratio_p1 = green_vol_p1 / red_vol_p1 if red_vol_p1 > 0 else 10.0
            pre_net_chg_p1 = (last_10['Close'].iloc[-1] - last_10['Close'].iloc[0]) / last_10['Close'].iloc[0] * 100
            
            latest_vol = df['Volume'].iloc[-1]
            max_10d_vol_p1 = df['Volume'].iloc[-11:-1].max()
            
            if (max_order_mult_p1 >= 1.8 and 
                avg_tightness_p1 <= 4.0 and 
                gr_ratio_p1 >= 2.5 and 
                pre_net_chg_p1 > 0 and 
                latest_vol <= max_10d_vol_p1):
                
                curr_price = round(df['Close'].iloc[-1], 2)
                d10_high = round(last_10['High'].max(), 2)
                
                # Updated Hyperlink Text: Only "View Chart"
                chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{symbol}"
                chart_hyperlink = f'=HYPERLINK("{chart_url}", "View Chart")'
                
                pre_surge_radar.append({
                    "Date": df['Date'].iloc[-1].strftime('%Y-%m-%d'),
                    "Symbol": symbol,
                    "Current_Price": curr_price,
                    "Watch_Trigger_High": d10_high,
                    "GR_Ratio": round(gr_ratio_p1, 2),
                    "Chart": chart_hyperlink
                })

        # -------------------------------------------------------------
        # PIPELINE 2: ACTIVE DELAYED SETUPS (Volume surge came, NO breakout yet)
        # -------------------------------------------------------------
        for i in range(n_rows - 10, n_rows):
            pre_10 = df.iloc[i-10:i]
            if len(pre_10) < 10:
                continue
                
            avg_turnover = pre_10['Turnover'].mean()
            if avg_turnover < 20000000:
                continue
                
            max_10d_vol = pre_10['Volume'].max()
            max_10d_high = pre_10['High'].max()
            
            curr_vol = df['Volume'].iloc[i]
            curr_close = df['Close'].iloc[i]
            curr_high = df['High'].iloc[i]
            
            if curr_vol > max_10d_vol and curr_close < max_10d_high and curr_high < max_10d_high:
                
                after_surge = df.iloc[i+1:]
                already_triggered = False
                if not after_surge.empty:
                    if (after_surge['High'] >= max_10d_high).any():
                        already_triggered = True
                
                if already_triggered:
                    continue
                
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
                    
                    # Updated Hyperlink Text: Only "View Chart"
                    chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{symbol}"
                    chart_hyperlink = f'=HYPERLINK("{chart_url}", "View Chart")'
                    
                    active_delayed_setups.append({
                        "Surge_Date": surge_date,
                        "Symbol": symbol,
                        "Entry_P": entry_p,
                        "SL_P": sl_p,
                        "Tgt_P": tgt_p,
                        "Chart": chart_hyperlink
                    })
                    break
    except Exception as e:
        return

if len(STOCKS) > 0:
    for idx, sym in enumerate(STOCKS, 1):
        print(f"[{idx}/{len(STOCKS)}] Processing Dual Pipeline for {sym}...")
        process_stock_pipelines(sym)

# -------------------------------------------------------------
# EXPORT TO GOOGLE SHEET TABS
# -------------------------------------------------------------

# Tab 1: PRE_SURGE_RADAR
if len(pre_surge_radar) > 0:
    df_p1 = pd.DataFrame(pre_surge_radar)
    df_p1['Date_dt'] = pd.to_datetime(df_p1['Date'])
    df_p1 = df_p1.sort_values(by='Date_dt', ascending=False).drop(columns=['Date_dt'])
    rows_p1 = df_p1.values.tolist()
    
    try:
        ws1 = sh.worksheet("PRE_SURGE_RADAR") if "PRE_SURGE_RADAR" in [x.title for x in sh.worksheets()] else sh.add_worksheet("PRE_SURGE_RADAR", 2000, 10)
        ws1.clear()
        ws1.append_row([f"PRE-SURGE RADAR (Volume Pending) | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws1.append_row(["Date", "Stock Name", "Current Price", "10D High Price", "Green/Red Vol Ratio", "Chart Link"])
        ws1.append_rows(rows_p1, value_input_option='USER_ENTERED')
        print("✅ Sheet 1 'PRE_SURGE_RADAR' updated successfully!")
    except Exception as e:
        print(f"Error updating PRE_SURGE_RADAR: {e}")

# Tab 2: ACTIVE_DELAYED_SETUPS
if len(active_delayed_setups) > 0:
    df_p2 = pd.DataFrame(active_delayed_setups)
    df_p2['Surge_Date_dt'] = pd.to_datetime(df_p2['Surge_Date'])
    df_p2 = df_p2.sort_values(by='Surge_Date_dt', ascending=False).drop(columns=['Surge_Date_dt'])
    rows_p2 = df_p2.values.tolist()
    
    try:
        ws2 = sh.worksheet("ACTIVE_DELAYED_SETUPS") if "ACTIVE_DELAYED_SETUPS" in [x.title for x in sh.worksheets()] else sh.add_worksheet("ACTIVE_DELAYED_SETUPS", 2000, 10)
        ws2.clear()
        ws2.append_row([f"ACTIVE DELAYED SETUPS (Volume Came, No Breakout) | {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws2.append_row(["Surge Date", "Stock Name", "Entry Price (10D High)", "SL Price (-3%)", "Target Price (+6%)", "Chart Link"])
        ws2.append_rows(rows_p2, value_input_option='USER_ENTERED')
        print("✅ Sheet 2 'ACTIVE_DELAYED_SETUPS' updated successfully!")
    except Exception as e:
        print(f"Error updating ACTIVE_DELAYED_SETUPS: {e}")
        
