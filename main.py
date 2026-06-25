import yfinance as yf
import pandas as pd
from datetime import datetime

STOCK = "RELAXO.NS"
TEST_DATE = "2026-06-09"

print(f"=== TESTING GHOST ON {STOCK} FOR {TEST_DATE} ===")

df = yf.download(STOCK, start="2025-01-01", end="2026-06-15", progress=False, auto_adjust=False)
nifty = yf.download("^NSEI", start="2025-01-01", end="2026-06-15", progress=False, auto_adjust=False)

if df.empty:
    print("BUG MIL GAYA: RELAXO ka data hi nahi aa raha Yahoo se.")
    exit()

# FIX 1: Nifty ko Series bana aur naam de
nifty_close = nifty['Close']
nifty_close.name = 'NIFTY'

# FIX 2: Join sahi tarike se
df['Vol_50MA'] = df['Volume'].rolling(window=50).mean()
df['High_40D'] = df['High'].rolling(window=40).max()
df['Low_40D'] = df['Low'].rolling(window=40).min()
df = df.join(nifty_close, how='left') # rename hata diya
df['RS_Line'] = df['Close'] / df['NIFTY']
df['RS_High_50D'] = df['RS_Line'].rolling(window=50).max()

if pd.to_datetime(TEST_DATE) not in df.index:
    print(f"9 JUNE KA DATA HI NAHI HAI RELAXO ME. Holiday tha ya data missing.")
    print(f"Last available: {df.index[-1].date()}")
    exit()

test_idx = df.index.get_loc(pd.to_datetime(TEST_DATE))
row = df.iloc[test_idx]
shelf_df = df.iloc[test_idx-30:test_idx]

print(f"\n--- {TEST_DATE} KA DATA ---")
print(f"Open: {row['Open']:.2f} | Close: {row['Close']:.2f} | High: {row['High']:.2f}")
print(f"High_40D: {row['High_40D']:.2f} | Low_40D: {row['Low_40D']:.2f}")
print(f"Volume: {int(row['Volume'])} | Vol_50MA: {int(row['Vol_50MA'])}")
print(f"RS_Line: {row['RS_Line']:.4f} | RS_50D_High: {row['RS_High_50D']:.4f}")

print(f"\n--- GHOST SHART CHECK ---")
# Shart 1: Shelf 30D
dry_vol_days = (shelf_df['Volume'] < shelf_df['Vol_50MA'] * 0.7).sum()
shelf_range = (shelf_df['High'].max() - shelf_df['Low'].min()) / shelf_df['Low'].min()
shelf_pass = dry_vol_days >= 18 and shelf_range <= 0.22
print(f"1. Shelf: {dry_vol_days}/30 din dry, Range: {shelf_range*100:.1f}% | Pass: {shelf_pass}")

# Shart 2: Down Dry
down_days = shelf_df[shelf_df['Close'] < shelf_df['Open']]
avg_down_vol = down_days['Volume'].mean() if len(down_days) > 0 else 0
dry_pass = avg_down_vol < row['Vol_50MA'] * 0.75
print(f"2. Down Vol: Avg {int(avg_down_vol)} vs 75% of 50MA {int(row['Vol_50MA']*0.75)} | Pass: {dry_pass}")

# Shart 3: Pivot
is_green = row['Close'] > row['Open'] * 1.005
near_high = row['Close'] >= row['High_40D'] * 0.97
vol_explosion = row['Volume'] > row['Vol_50MA'] * 1.3
pivot_pass = is_green and near_high and vol_explosion
print(f"3. Pivot: Green={is_green}, Near40D_High={near_high}, Vol1.3x={vol_explosion} | Pass: {pivot_pass}")

# Shart 4: RS
rs_pass = row['RS_Line'] >= row['RS_High_50D'] * 0.98
print(f"4. RS New High: {rs_pass}")

score = sum([shelf_pass, dry_pass, pivot_pass, rs_pass])
print(f"\n=== FINAL SCORE: {score}/4 ===")

if score >= 2:
    entry = row['Close']
    sl = row['Low_40D'] * 0.98
    target = entry + 3 * (entry - sl)
    print(f"GHOST SIGNAL BANTA HAI! Entry:{entry:.2f} SL:{sl:.2f} Target:{target:.2f}")
    print("MATLAB FULL SCANNER CODE ME BUG HAI. SINGLE STOCK PE CHAL RAHA.")
else:
    print("SIGNAL NAHI BANTA RELAXO ME 9 JUNE KO.")
    print("MATLAB TERA WALA EXAMPLE YA TO GALAT DATE KA THA YA LOGIC BAKWAS HAI.")
