import yfinance as yf
import pandas as pd
import numpy as np
from itertools import product

STOCK = "RELIANCE.NS"
TARGET = 10.0
SL = 5.0
HOLD = 20

print("=== GRID SEARCH FOR BEST DNA ===", flush=True)

df = yf.download(STOCK, period="max", progress=False, auto_adjust=True)
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

# Test karne wale sab combos
range_list = [8, 10, 12, 15, 20]
vol_list = [0.8, 1.0, 1.2, 1.5]
hl_list = [3, 4, 5, 6]
dry_list = [2, 3, 4, 5]

results = []
total_combos = len(range_list) * len(vol_list) * len(hl_list) * len(dry_list)
print(f"Testing {total_combos} combinations...", flush=True)

for r_max, v_max, hl_min, dry_min in product(range_list, vol_list, hl_list, dry_list):
    wins = losses = total = 0
    i = 10

    while i < len(df) - HOLD:
        window = df.iloc[i-10:i]
        today = df.iloc[i]

        if window['Volume'].mean() == 0:
            i += 1
            continue

        range_10d = (window['High'].max() - window['Low'].min()) / window['Low'].min() * 100
        vol_ratio = today['Volume'] / window['Volume'].mean()
        higher_lows = (window['Low'].diff() > 0).sum()
        vol_dry = (window['Volume'] < window['Volume'].mean() * 0.8).sum()

        if range_10d <= r_max and vol_ratio <= v_max and higher_lows >= hl_min and vol_dry >= dry_min:
            total += 1
            entry = today['Close']
            result = 'NEUTRAL'

            for j in range(i+1, min(i+HOLD+1, len(df))):
                if df['Low'].iloc[j] <= entry * (1 - SL/100):
                    result = 'LOSS'
                    break
                if df['High'].iloc[j] >= entry * (1 + TARGET/100):
                    result = 'WIN'
                    break

            if result == 'WIN': wins += 1
            if result == 'LOSS': losses += 1
            i = i + HOLD
        else:
            i += 1

    if total >= 50: # Min 50 trades hona chahiye
        winrate = round(wins / total * 100, 1)
        expectancy = round((winrate/100 * TARGET) + ((100-winrate)/100 * -SL), 2)
        results.append({
            'Range': f"<{r_max}%", 'Vol': f"<{v_max}", 'HL': f">={hl_min}", 'Dry': f">={dry_min}",
            'Signals': total, 'Wins': wins, 'Loss': losses, 'WinRate': winrate, 'Expectancy': expectancy
        })

# Top 10 Best Combos
df_res = pd.DataFrame(results).sort_values(['Expectancy', 'WinRate'], ascending=False)
print("\n=== TOP 10 BEST DNA COMBOS ===", flush=True)
print(df_res.head(10).to_string(index=False))
