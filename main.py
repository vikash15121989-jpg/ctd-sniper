import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

def find_spring_creek_setup(stock_list):
    results = []
    
    for stock in stock_list:
        try:
            # 6 mahine ka data lelo
            df = yf.download(stock + '.NS', period='6mo', progress=False)
            if len(df) < 90: continue
            
            df['Vol_50'] = df['Volume'].rolling(50).mean()
            
            # 1. SPRING FIND KARO = 90 din ka lowest low
            df_90d = df.tail(90)
            spring_low = df_90d['Low'].min()
            spring_date = df_90d['Low'].idxmin().strftime('%d/%m/%Y')
            spring_idx = df_90d['Low'].idxmin()
            
            # 2. CREEK FIND KARO = Spring se pehle ka highest high
            df_before_spring = df.loc[:spring_idx].tail(90)  # Spring se pehle 90 din
            if len(df_before_spring) < 20: continue
            creek_high = df_before_spring['High'].max()
            creek_date = df_before_spring['High'].idxmax().strftime('%d/%m/%Y')
            
            # 3. LIQUIDITY CHECK = Recent 50 din ka avg
            last_candle = df.iloc[-1]
            avg_volume = last_candle['Vol_50']
            avg_turnover = avg_volume * last_candle['Close']  # Daily turnover in Rs
            
            liquidity_ok = avg_turnover > 50000000 and avg_volume > 100000  # 5Cr + 1Lakh shares
            
            # 4. KYA SPRING RECENT HAI? = Last 60 din me bana ho
            days_since_spring = (df.index[-1] - spring_idx).days
            recent_spring = days_since_spring <= 60
            
            # 5. PRICE ABHI KAHA HAI CREEK SE
            current_price = last_candle['Close']
            distance_from_creek = ((current_price - creek_high) / creek_high) * 100
            
            if liquidity_ok and recent_spring:
                results.append({
                    'Stock': stock,
                    'Spring_Date': spring_date,
                    'Spring_Low': round(spring_low, 2),
                    'Creek_Date': creek_date, 
                    'Creek_High': round(creek_high, 2),
                    'CMP': round(current_price, 2),
                    'Dist_from_Creek_%': round(distance_from_creek, 2),
                    'Avg_Turnover_Cr': round(avg_turnover/10000000, 1),
                    'Days_Since_Spring': days_since_spring,
                    'Status': 'WATCH' if current_price < creek_high else 'BREAKOUT?'
                })
                
        except: continue
    
    return pd.DataFrame(results)

# USE KARNE KA TARIKA
nifty50 = ['RELIANCE', 'TCS', 'INFY', 'HDFC', 'ICIBANK', 'NETWEB', 'ADANIENT']
df = find_spring_creek_setup(nifty50)
df = df.sort_values('Days_Since_Spring')  # Latest spring wale upar
print(df)
