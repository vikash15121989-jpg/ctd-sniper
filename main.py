import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

def find_spring_creek_setup(stock_list):
    results = []
    
    for stock in stock_list:
        try:
            # .NS add karo agar pehle se nahi hai
            ticker = stock if '.NS' in stock else stock + '.NS'
            df = yf.download(ticker, period='6mo', progress=False)
            
            if df.empty or len(df) < 90: 
                print(f"{stock}: Data nahi mila ya 90 din se kam hai")
                continue
            
            df['Vol_50'] = df['Volume'].rolling(50).mean()
            
            # 1. SPRING FIND KARO
            df_90d = df.tail(90)
            spring_low = df_90d['Low'].min()
            spring_date = df_90d['Low'].idxmin()
            spring_idx = df_90d['Low'].idxmin()
            
            # 2. CREEK FIND KARO
            df_before_spring = df.loc[:spring_idx].tail(90)
            if len(df_before_spring) < 20: continue
            creek_high = df_before_spring['High'].max()
            creek_date = df_before_spring['High'].idxmax()
            
            # 3. LIQUIDITY CHECK
            last_candle = df.iloc[-1]
            avg_volume = last_candle['Vol_50']
            if pd.isna(avg_volume): continue  # NaN check
            
            avg_turnover = avg_volume * last_candle['Close']
            liquidity_ok = avg_turnover > 50000000 and avg_volume > 100000
            
            # 4. RECENT SPRING CHECK
            days_since_spring = (df.index[-1] - spring_idx).days
            recent_spring = days_since_spring <= 60
            
            # 5. PRICE LOCATION
            current_price = last_candle['Close']
            distance_from_creek = ((current_price - creek_high) / creek_high) * 100
            
            if liquidity_ok and recent_spring:
                results.append({
                    'Stock': stock.replace('.NS',''),
                    'Spring_Date': spring_date.strftime('%d/%m/%Y'),
                    'Spring_Low': round(spring_low, 2),
                    'Creek_Date': creek_date.strftime('%d/%m/%Y'), 
                    'Creek_High': round(creek_high, 2),
                    'CMP': round(current_price, 2),
                    'Dist_from_Creek_%': round(distance_from_creek, 2),
                    'Avg_Turnover_Cr': round(avg_turnover/10000000, 1),
                    'Days_Since_Spring': days_since_spring,
                    'Status': 'WATCH' if current_price < creek_high else 'BREAKOUT?'
                })
                
        except Exception as e:
            print(f"{stock}: Error - {e}")
            continue
    
    # FIX: Khali df ka check daal diya
    if not results:
        print("Koi bhi stock criteria me pass nahi hua bhai")
        return pd.DataFrame()  # Khali df return karo
    
    df = pd.DataFrame(results)
    df = df.sort_values('Days_Since_Spring')  # Ab error nahi aayega
    return df

# SAHI TICKER NAMES USE KAR
nifty50 = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'NETWEB', 'ADANIENT']
df = find_spring_creek_setup(nifty50)
print(df)
