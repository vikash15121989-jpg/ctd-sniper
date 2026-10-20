import pandas as pd
from datetime import datetime, timedelta
from nselib import capital_market

print("=== RELAXED SMART MONEY BACKTESTER ===")

def run_backtest(symbol):
    try:
        print(f"Backtesting {symbol}...")
        to_date = datetime.now().strftime('%d-%m-%Y')
        from_date = (datetime.now() - timedelta(days=365)).strftime('%d-%m-%Y')
        
        df = capital_market.price_volume_data(symbol=symbol, from_date=from_date, to_date=to_date)
        
        if df is None or df.empty or len(df) < 60:
            return []
            
        # Clean and remove duplicate columns
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
            return []

        df['Date'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        df = df.sort_values('Date').reset_index(drop=True)
        
        for col in ['Volume', 'Trades', 'Close', 'High', 'Low']:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
                
        df = df.dropna(subset=['Volume', 'Trades', 'Close', 'High', 'Low'])
        
        # 1. Indicators Calculation
        df['Avg_Order_Size'] = df['Volume'] / df['Trades']
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['Avg_Size_MA20'] = df['Avg_Order_Size'].rolling(20).mean()
        df['SMA50'] = df['Close'].rolling(50).mean()
        
        trades = []
        i = 50
        n_rows = len(df)
        
        while i < n_rows - 15:
            close = df['Close'].iloc[i]
            high = df['High'].iloc[i]
            low = df['Low'].iloc[i]
            vol = df['Volume'].iloc[i]
            vol_ma = df['Vol_MA20'].iloc[i]
            avg_size = df['Avg_Order_Size'].iloc[i]
            avg_size_ma = df['Avg_Size_MA20'].iloc[i]
            sma50 = df['SMA50'].iloc[i]
            
            if pd.isna(sma50) or vol_ma == 0 or avg_size_ma == 0:
                i += 1
                continue
            
            # Relaxed Smart Money Trigger Rules:
            vol_surge = vol >= (1.4 * vol_ma)
            big_order_size = avg_size >= (1.2 * avg_size_ma)
            
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            delivery_close = range_pos >= 0.65
            
            trend_ok = close > sma50
            
            if vol_surge and big_order_size and delivery_close and trend_ok:
                entry = close
                sl = low * 0.98  # 2% below day low
