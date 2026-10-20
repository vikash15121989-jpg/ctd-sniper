import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market
import gspread
import json
import os
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== NSE QUIET ACCUMULATION + STRUCTURE SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

# Google Sheets Connection via GitHub Secrets
try:
    gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
    sh = gc.open("CTD_Sniper")
    # Watchlist tab se stocks uthana
    raw = sh.worksheet("Watchlist").col_values(1)
    STOCKS = [s.strip().upper() for s in raw if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]][:60]
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    STOCKS = ["RELIANCE", "TCS", "SBIN", "INFY", "LT", "AXISBANK", "CHOLAFIN", "KAJARIACER", "ELECON"]

scans = []
to_date = datetime.now().strftime('%d-%m-%Y')
from_date = (datetime.now() - timedelta(days=120)).strftime('%d-%m-%Y')

for symbol in STOCKS:
    try:
        print(f"Scanning {symbol}...")
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 50:
            continue
            
        # Clean column names and remove duplicate columns
        df.columns = [str(c).strip().title() for c in df.columns]
        df = df.loc[:, ~df.columns.duplicated()]
        
        # Column mapping
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
            continue

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        if len(df) < 40:
            continue

        # Calculations
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['SMA50'] = df['Close'].rolling(50).mean()
        df['Prev_Close'] = df['Close'].shift(1)
        df['Daily_Return'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close'] * 100
        
        # Resistance / Structure Filter (20-day High tracking for breakout readiness)
        df['High_20'] = df['High'].rolling(20).max()
        
        # Latest row data
        last_row = df.iloc[-1]
        
        close = last_row['Close']
        sma50 = last_row['SMA50']
        avg_size = last_row['Avg_Order_Size']
        avg_size_ma = last_row['Avg_Size_MA20']
        daily_ret = last_row['Daily_Return']
        vol = last_row['Volume']
        vol_ma = last_row['Vol_MA20']
        high_20 = last_row['High_20']
        
        if pd.isna(sma50) or pd.isna(avg_size_ma) or avg_size_ma == 0 or pd.isna(vol_ma) or pd.isna(high_20):
            continue
        
        # --- ENHANCED QUIET ACCUMULATION + STRUCTURE LOGIC ---
        # 1. Trend Filter: Price above 50 SMA
        trend_ok = close > sma50
        
        # 2. Stealth / Institutional Footprint: Order size is high (>= 1.3x of 20-day average)
        stealth_buying = avg_size >= (1.3 * avg_size_ma)
        
        # 3. Quiet Price Action: Stock has NOT exploded yet (-1% to +3.5%)
        price_is_quiet = -1.0 <= daily_ret <= 3.5
        
        # 4. Volume Support
        volume_ok = vol >= (0.8 * vol_ma)
        
        # 5. Structure Filter: Price is coiling near its 20-day High (within 5% of 20-day high resistance)
        near_resistance = close >= (0.95 * high_20)

        if trend_ok and stealth_buying and price_is_quiet and volume_ok and near_resistance:
            scans.append([
                symbol,
                last_row['Date'].strftime('%Y-%m-%d'),
                round(close, 2),
                round(daily_ret, 2),
                round(vol / 100000, 2), # Volume in Lakhs
                round(avg_size / avg_size_ma, 2) # Stealth Size Multiple
            ])
    except Exception as e:
        print(f"Error processing {symbol}: {e}")
        continue

# Export to Google Sheet
try:
    if len(scans) > 0:
        df_out = pd.DataFrame(scans, columns=["Stock", "Date", "Close", "Daily_Return_%", "Volume_Lakhs", "Stealth_Size_Multiple"])
        ws = sh.worksheet("QUIET_ACCUMULATION") if "QUIET_ACCUMULATION" in [x.title for x in sh.worksheets()] else sh.add_worksheet("QUIET_ACCUMULATION", 1000, 10)
        ws.clear()
        ws.append_row([f"Quiet Accumulation + Structure Signals | Updated: {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"])
        ws.append_row(["Stock", "Date", "Close", "Daily Return %", "Volume (Lakhs)", "Stealth Size Multiple"])
        ws.append_rows(df_out.values.tolist())
        print(f"✅ Success! Found {len(scans)} setups and updated Google Sheet.")
    else:
        print("No setups found for today.")
except Exception as e:
    print(f"Error updating Google Sheet: {e}")
    
