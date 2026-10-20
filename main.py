import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== NSE SMART MONEY SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

# Google Sheets Connection via GitHub Secrets
try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]][:50]
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = ["RELIANCE", "TCS", "SBIN", "INFY", "LT"] # Fallback list

scans = []
to_date = datetime.now().strftime('%d-%m-%Y')
from_date = (datetime.now() - timedelta(days=120)).strftime('%d-%m-%Y')

for symbol in STOCKS:
    try:
        print(f"Fetching data for {symbol}...")
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 30:
            print(f"Insufficient data for {symbol}")
            continue
            
        # Clean column names (Remove spaces and standardize)
        df.columns = [str(c).strip().title() for c in df.columns]
        
        # Mapping column variations safely
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
        
        required_cols = ['Volume', 'Trades', 'Close', 'High', 'Low', 'Date']
        if not all(col in df.columns for col in required_cols):
            print(f"Missing required columns in {symbol}. Found: {list(df.columns)}")
            continue

        # Convert date and sort
        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        # Numeric conversion
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        if len(df) < 25:
            continue

        # 1. Calculate Average Order Size (Volume / No. of Trades)
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        
        # 2. Moving Averages
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['SMA50'] = df['Close'].rolling(50).mean()
        
        # Latest row
        last_row = df.iloc[-1]
        
        close = last_row['Close']
        high = last_row['High']
        low = last_row['Low']
        vol = last_row['Volume']
        vol_ma = last_row['Vol_MA20']
        avg_size = last_row['Avg_Order_Size']
        avg_size_ma = last_row['Avg_Size_MA20']
        sma50 = last_row['SMA50']
        
        if pd.isna(vol_ma) or vol_ma == 0 or pd.isna(avg_size_ma) or avg_size_ma == 0:
            continue
        
        # Smart Money Logic Conditions:
        vol_surge = vol >= (1.8 * vol_ma)
        big_order_size = avg_size >= (1.4 * avg_size_ma)
        
        day_range = high - low
        range_pos = (close - low) / day_range if day_range > 0 else 0
        delivery_close = range_pos >= 0.75
        
        trend_ok = (not pd.isna(sma50)) and (close > sma50)
        
        if vol_surge and big_order_size and delivery_close and trend_ok:
            scans.append([
                symbol,
                last_row['Date'].strftime('%Y-%m-%d'),
                round(close, 2),
                round(vol / 100000, 2), # Volume in Lakhs
                round(vol / vol_ma, 2), # Volume Multiple
                round(avg_size / avg_size_ma, 2) # Order Size Multiple
            ])
    except Exception as e:
        print(f"Error processing {symbol}: {e}")
        continue

# Export to Google Sheet
try:
    if len(scans) > 0:
        df_out = pd.DataFrame(scans, columns=["Stock", "Date", "Close", "Volume_Lakhs", "Vol_Multiple", "Avg_Size_Multiple"])
        ws = sh.worksheet("SMART_MONEY_SIGNALS") if "SMART_MONEY_SIGNALS" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SMART_MONEY_SIGNALS", 1000, 10)
        ws.clear()
        ws.append_row([f"Smart Money Accumulation Signals (nselib) | Updated: {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Date", "Close", "Volume (Lakhs)", "Vol Multiple", "Avg Size Multiple"])
        ws.append_rows(df_out.values.tolist())
        print(f"✅ Success! Found {len(scans)} signals and updated Google Sheet.")
    else:
        print("No institutional accumulation signals found for today.")
except Exception as e:
    print(f"Error updating Google Sheet: {e}")
    
