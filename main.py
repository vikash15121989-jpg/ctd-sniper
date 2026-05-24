import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# CONFIG - Yahi settings best hain
PRICE_MIN, PRICE_MAX = 100, 3000
MIN_AVG_TURNOVER_CR = 2  # 2 Cr se kam patli stock nahi
MIN_VOL_30D = 50000      # 30 din ka avg volume minimum
PROB_SCORE_MIN = 90      # 90% se kam probability wala reject

def load_nse_stocks():
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    df = pd.read_csv(url)
    return [s + ".NS" for s in df['SYMBOL']]

def check_spring_setup(ticker):
    try:
        end = datetime.now()
        start = end - timedelta(days=365)  # 1 saal ka data
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        
        if len(df) < 200: return None
        df.dropna(inplace=True)
        
        # Latest data
        c = df['Close'].iloc[-1]
        if not (PRICE_MIN < c < PRICE_MAX): return None
        
        # 1. 52 Week Low ke paas hai? - Daba hua stock
        low_52w = df['Low'][-252:].min()
        if c > low_52w * 1.15: return None  # 15% se zyada upar to reject
        
        # 2. 30 Din Tight Range - Spring dab rahi hai
        high_30d = df['High'][-30:].max()
        low_30d = df['Low'][-30:].min()
        range_pct = (high_30d - low_30d) / c * 100
        if range_pct > 8: return None  # 8% se zyada range = loose
        
        # 3. Volume Death - Koi interest nahi
        vol_30d = df['Volume'][-30:].mean()
        vol_90d = df['Volume'][-90:].mean()
        if vol_30d > vol_90d * 0.3: return None  # Volume abhi bhi zyada hai
        if vol_30d < MIN_VOL_30D: return None    # Bilkul hi illiquid
        
        # 4. Higher Lows - Smart money jama kar raha
        lows = df['Low'][-30:].rolling(5).min().dropna()
        if len(lows) < 3: return None
        if not (lows.iloc[-3] <= lows.iloc[-2] <= lows.iloc[-1]): return None
        
        # 5. 200 EMA Test - Bada level
        ema_200 = df['Close'].ewm(span=200).mean().iloc[-1]
        if abs(c - ema_200) / ema_200 > 0.03: return None  # 3% se door hai
        
        # 6. False Breakdown hua? - Bears haar gaye
        below_ema = df[df['Close'] < ema_200][-30:]
        if len(below_ema) == 0: return None  # Kabhi neeche gaya hi nahi
        
        # 7. Turnover Check
        avg_turnover = vol_30d * c / 1e7  # Crore me
        if avg_turnover < MIN_AVG_TURNOVER_CR: return None
        
        # Trigger Level = 30 Day High + 0.5% buffer
        trigger = high_30d * 1.005
        
        # Probability Score Calculator
        score = 70  # Base
        if range_pct < 5: score += 10      # Aur tight range
        if vol_30d < vol_90d * 0.15: score += 10  # Volume ekdum dead
        if c > low_52w * 1.05: score += 5   # 52w low ke bilkul paas
        if len(below_ema) > 0: score += 5   # False breakdown confirm
        
        if score < PROB_SCORE_MIN: return None
        
        return {
            'Stock': ticker.replace('.NS', ''),
            'Close': round(c, 2),
            'Trigger_Level': round(trigger, 2),
            'Prob_Score': score,
            'Target_1': round(trigger * 1.10, 2),
            'Target_2': round(trigger * 1.20, 2),
            'SL': round(trigger * 0.97, 2),
            'Range_30D': f"{range_pct:.1f}%",
            'Vol_Ratio': f"{vol_30d/vol_90d:.2f}"
        }
        
    except: return None

# MAIN
if __name__ == "__main__":
    print("Starting Spring-Breakout Scanner...")
    stocks = load_nse_stocks()
    results = []
    
    for i, stock in enumerate(stocks):
        if i % 200 == 0: print(f"Scanning {i}/{len(stocks)}...")
        res = check_spring_setup(stock)
        if res: results.append(res)
    
    if results:
        df = pd.DataFrame(results).sort_values('Prob_Score', ascending=False)
        df.to_csv('spring_breakout_signals.csv', index=False)
        print(f"\nFound {len(df)} high-probability setups:")
        print(df[['Stock', 'Trigger_Level', 'Prob_Score', 'Target_1', 'SL']].head(10))
    else:
        print("\nNo Spring-Breakout setup today. Market me tight range stocks nahi hain.")
        pd.DataFrame().to_csv('spring_breakout_signals.csv', index=False)
