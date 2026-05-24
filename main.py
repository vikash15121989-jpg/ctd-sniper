import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

def get_nse_stocks():
    try:
        # stocks.csv se A column padh
        df_stocks = pd.read_csv('stocks.csv', header=None)
        stocks = df_stocks[0].dropna().astype(str).str.strip().str.upper().tolist()
        print(f"Total {len(stocks)} stocks loaded from stocks.csv")
        return stocks
    except Exception as e:
        print(f"stocks.csv nahi mila: {e}")
        print("Default 10 stocks use kar raha hu")
        return ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN','ITC','LT','AXISBANK','KOTAKBANK']

def detect_spring(df):
    try:
        if len(df) < 100: return None

        df['EMA20'] = df['Close'].ewm(span=20).mean()
        df['EMA50'] = df['Close'].ewm(span=50).mean()
        df['EMA200'] = df['Close'].ewm(span=200).mean()
        df['Vol_50D'] = df['Volume'].rolling(50).mean()

        # Last 30 candles me spring dhoondo
        for i in range(-30, -5):
            candle = df.iloc[i]
            prev_candles = df.iloc[i-5:i]
            if prev_candles.empty: continue

            # Support: EMA ya Recent Low
            supports = [
                candle['EMA20'],
                candle['EMA50'],
                candle['EMA200'],
                prev_candles['Low'].min()
            ]

            spring_support = None
            for sup in supports:
                # Spring: Low ne support toda, Close wapas upar
                if candle['Low'] < sup * 0.99 and candle['Close'] > sup:
                    spring_support = sup
                    spring_day = i
                    break

            if spring_support is None: continue

            # Spring ke baad range + absorption
            post_spring = df.iloc[spring_day:]
            if len(post_spring) < 5: continue

            range_high = post_spring['High'].max()
            range_low = post_spring['Low'].min()
            range_pct = (range_high - range_low) / range_low * 100
            if range_pct > 12: continue

            # Volume: Spring pe high, ab dead
            spring_vol = candle['Volume']
            avg_vol_after = post_spring['Volume'].tail(10).mean()
            avg_vol_50d = candle['Vol_50D']

            if spring_vol < avg_vol_50d * 1.2: continue
            if avg_vol_after > avg_vol_50d * 0.6: continue

            # Higher Lows check
            recent_lows = post_spring['Low'].tail(10).rolling(3).min().dropna()
            if len(recent_lows) >= 2 and recent_lows.iloc[-1] < recent_lows.iloc[-2]:
                continue

            # PASS
            current_price = df['Close'].iloc[-1]
            score = 75
            if range_pct < 8: score += 10
            if avg_vol_after < avg_vol_50d * 0.3: score += 10
            if current_price > candle['High']: score += 5

            return {
                'Trigger_Level': round(range_high, 2),
                'Prob_Score': min(score, 99),
                'Target_1': round(range_high * 1.10, 2),
                'Target_2': round(range_high * 1.20, 2),
                'SL': round(spring_support * 0.98, 2),
                'Spring_Type': f"Near {round(spring_support,0)}",
                'Range_Days': len(post_spring)
            }
        return None
    except: return None

# Main Scanner
stocks = get_nse_stocks()
signals = []
print("Starting Pure Spring + Absorption Scanner...")

for i, stock in enumerate(stocks):
    if i % 10 == 0: print(f"Scanning {i}/{len(stocks)}...")
    try:
        df = yf.download(f"{stock}.NS", period="6mo", interval="1d", progress=False)
        if df.empty: continue
        result = detect_spring(df)
        if result:
            result['Stock'] = stock
            signals.append(result)
    except: continue

# CSV Save
df_out = pd.DataFrame(signals)
if not df_out.empty:
    df_out = df_out[['Stock','Trigger_Level','Prob_Score','Target_1','Target_2','SL','Spring_Type','Range_Days']]
    df_out = df_out.sort_values('Prob_Score', ascending=False)
    df_out.to_csv("spring_breakout_signals.csv", index=False)
    print(f"\nFound {len(df_out)} Spring setups:")
    print(df_out.to_string(index=False))
else:
    pd.DataFrame().to_csv("spring_breakout_signals.csv", index=False)
    print("\nNo Spring setup found today.")
